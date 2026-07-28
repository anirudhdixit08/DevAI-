import { Router } from "express";
import {
  cancelProjectRun,
  createProjectRun,
  restartProjectPreview,
  stopProjectPreview,
  streamProjectEvents,
  submitProjectInput,
} from "../services/orchestratorClient.js";
import {
  getProjectMetadata,
  listProjects,
  saveProjectMetadata,
  clearActivePreviewForUser,
  updateProjectFromEvent,
} from "../services/projectStore.js";
import { createProjectZipBuffer } from "../services/projectZip.js";
import { normalizePublicPreviewUrl, publicUrlForPort } from "../utils/publicUrls.js";

const router = Router();

function getUserId(req) {
  return req.user?.user_id;
}

function getSandboxId(project) {
  return project?.last_state?.sandboxId || project?.last_state?.sandbox_id || project?.sandbox_id || null;
}

async function stopPreviewBestEffort(project) {
  const sandboxId = getSandboxId(project);
  if (!sandboxId) return null;
  try {
    return await stopProjectPreview(project.project_id, sandboxId);
  } catch (error) {
    console.warn(`Preview cleanup skipped for ${project.project_id}: ${error.message}`);
    return { stopped: false, warning: error.message };
  }
}

function getPreviewPatch(req, project, result, running) {
  const frontendPort = result.frontendPort || project?.preview_frontend_port || project?.last_state?.previewFrontendPort || null;
  const backendPort = result.backendPort || project?.preview_backend_port || project?.last_state?.previewBackendPort || null;
  return {
    sandbox_id: getSandboxId(project),
    preview_frontend_port: frontendPort,
    preview_backend_port: backendPort,
    preview_frontend_url: normalizePublicPreviewUrl(result.frontendUrl || project?.preview_frontend_url, frontendPort, req)
      || publicUrlForPort(frontendPort, req),
    preview_backend_url: normalizePublicPreviewUrl(result.backendUrl || project?.preview_backend_url, backendPort, req)
      || publicUrlForPort(backendPort, req),
    preview_running: running,
  };
}

function ownsProject(req, project) {
  return project?.user_id === getUserId(req);
}

router.get("/", async (req, res, next) => {
  try {
    const projects = await listProjects(getUserId(req));
    res.json({ projects });
  } catch (error) {
    next(error);
  }
});

router.post("/", async (req, res, next) => {
  try {
    if (!req.body?.requirement || typeof req.body.requirement !== "string") {
      res.status(400).json({ error: "requirement is required" });
      return;
    }

    const payload = {
      requirement: req.body.requirement.trim(),
      user_id: getUserId(req),
      token_budget_usd: req.body.token_budget_usd ?? 2.0,
    };
    const activeProject = (await listProjects(payload.user_id)).find((project) => (
      project.status === "running" || project.status === "queued"
    ));
    if (activeProject) {
      res.status(409).json({
        error: "A project is already building. Cancel or finish it before launching another project.",
        active_project_id: activeProject.project_id,
      });
      return;
    }
    const stoppedProjects = await clearActivePreviewForUser(payload.user_id);
    for (const stoppedProject of stoppedProjects) {
      await stopPreviewBestEffort(stoppedProject);
    }

    const run = await createProjectRun(payload);
    const project = await saveProjectMetadata({
      project_id: run.project_id,
      user_id: payload.user_id,
      requirement: payload.requirement,
      status: run.status,
    });
    res.status(201).json({ ...run, project });
  } catch (error) {
    next(error);
  }
});

router.get("/:projectId", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }
    if (!ownsProject(req, project)) {
      res.status(403).json({ error: "forbidden" });
      return;
    }
    res.json(project);
  } catch (error) {
    next(error);
  }
});

router.get("/:projectId/download", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }
    if (!ownsProject(req, project)) {
      res.status(403).json({ error: "forbidden" });
      return;
    }

    const sandboxId = getSandboxId(project);
    if (!sandboxId) {
      res.status(400).json({ error: "project has no sandbox id yet" });
      return;
    }

    const zipBuffer = await createProjectZipBuffer(sandboxId);
    res.setHeader("Content-Type", "application/zip");
    res.setHeader("Content-Disposition", `attachment; filename="${project.project_id}-code.zip"`);
    res.setHeader("Content-Length", String(zipBuffer.length));
    res.send(zipBuffer);
  } catch (error) {
    next(error);
  }
});

router.get("/:projectId/events", async (req, res, next) => {
  const controller = new AbortController();

  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }

    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.flushHeaders?.();

    req.on("close", () => controller.abort());

    await streamProjectEvents(req.params.projectId, (event) => {
      updateProjectFromEvent(req.params.projectId, event).catch(() => {});
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    }, {
      signal: controller.signal,
    });
    res.end();
  } catch (error) {
    if (controller.signal.aborted || error.name === "AbortError") return;
    next(error);
  }
});

router.post("/:projectId/input", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }
    if (!["pm_clarification", "escalation"].includes(req.body?.type)) {
      res.status(400).json({ error: "type must be pm_clarification or escalation" });
      return;
    }

    const result = await submitProjectInput(req.params.projectId, req.body);
    res.json(result);
  } catch (error) {
    next(error);
  }
});

router.post("/:projectId/cancel", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }

    const result = await cancelProjectRun(req.params.projectId);
    const updatedProject = await saveProjectMetadata({
      ...project,
      status: "cancelled",
      preview_running: false,
      last_event_type: "run.cancelled",
      last_event_node: "gateway",
      last_message: result.cancelled ? "Cancel requested" : "No active workflow task found",
    });
    res.json({ ...result, project: updatedProject });
  } catch (error) {
    next(error);
  }
});

router.post("/:projectId/preview/stop", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }
    const stoppedProjects = await clearActivePreviewForUser(getUserId(req), req.params.projectId);
    const stoppedResults = [];
    for (const stoppedProject of stoppedProjects) {
      stoppedResults.push(await stopPreviewBestEffort(stoppedProject));
    }
    const result = await stopProjectPreview(req.params.projectId, getSandboxId(project));
    const updatedProject = await saveProjectMetadata({
      ...project,
      preview_running: false,
      last_event_type: "preview.stopped",
      last_event_node: "gateway",
      last_message: "Preview containers stopped",
    });
    res.json({
      ...result,
      stopped_projects: stoppedProjects.map((item) => item.project_id),
      stopped_results: stoppedResults,
      project: updatedProject,
    });
  } catch (error) {
    next(error);
  }
});

router.post("/:projectId/preview/restart", async (req, res, next) => {
  try {
    const project = await getProjectMetadata(req.params.projectId);
    if (!project) {
      res.status(404).json({ error: "project not found" });
      return;
    }
    const sandboxId = getSandboxId(project);
    if (!sandboxId) {
      res.status(400).json({ error: "project has no sandbox id yet" });
      return;
    }
    const stoppedProjects = await clearActivePreviewForUser(getUserId(req), req.params.projectId);
    for (const stoppedProject of stoppedProjects) {
      await stopPreviewBestEffort(stoppedProject);
    }
    const result = await restartProjectPreview(req.params.projectId, sandboxId, {
      userId: getUserId(req),
      backendPort: project.preview_backend_port,
      frontendPort: project.preview_frontend_port,
    });
    const updatedProject = await saveProjectMetadata({
      ...project,
      ...getPreviewPatch(req, project, result, Boolean(result.started)),
      last_event_type: result.started ? "preview.started" : "preview.failed",
      last_event_node: "gateway",
      last_message: result.started
        ? `Preview containers restarted at ${normalizePublicPreviewUrl(result.frontendUrl || project.preview_frontend_url, result.frontendPort || project.preview_frontend_port, req) || "assigned port"}`
        : (result.errors || ["Preview restart failed"]).join("; "),
    });
    res.json({ ...result, stopped_projects: stoppedProjects.map((item) => item.project_id), project: updatedProject });
  } catch (error) {
    next(error);
  }
});

router.use((error, _req, res, _next) => {
  res.status(error.statusCode || 500).json({ error: error.message });
});

export default router;
