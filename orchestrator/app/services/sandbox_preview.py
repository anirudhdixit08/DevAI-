from __future__ import annotations

from typing import Any

from .sandbox_database import _external_project_db_enabled, _initialize_external_project_db, _initialize_postgres_from_project_sql
from .sandbox_files import _info, get_sandbox_info, stop_sandbox_containers
from .sandbox_process import _docker_exec, schedule_preview_auto_stop, stop_active_preview_for_user
from .sandbox_runtime import reconnect_sandbox
from .sandbox_state import NPM_INSTALL_TIMEOUT_MS, _active_preview_by_user, _sandboxes


def start_sandbox_servers(sandbox_id: str) -> dict[str, Any]:
    info = _info(sandbox_id)
    if not info:
        return {"started": False, "errors": ["Sandbox not found"]}

    errors: list[str] = []
    outputs: list[str] = []
    try:
        if _external_project_db_enabled(info.db_type):
            _initialize_external_project_db(sandbox_id, info.backend_path)
        elif info.db_container_id and info.db_type == "postgres":
            _initialize_postgres_from_project_sql(info.db_container_id, info.backend_path)
    except Exception as error:
        errors.append(f"database initialization failed: {error}")

    if info.backend_container_id:
        backend = _docker_exec(
            info.backend_container_id,
            "node -e \"fetch('http://127.0.0.1:5000/api/health').then(()=>process.exit(0)).catch(()=>process.exit(1))\" "
            "|| (pkill -f 'node src/index.js' 2>/dev/null || true; pkill -f 'npm start' 2>/dev/null || true; "
            "sleep 1; cd /app/backend && (nohup npm start > /tmp/aidev-backend.log 2>&1 &))",
            10000,
        )
        if backend["exitCode"] == 0:
            outputs.append("backend started")
        else:
            errors.append(backend["stderr"] or backend["stdout"] or "backend start failed")

    if info.frontend_container_id:
        frontend = _docker_exec(
            info.frontend_container_id,
            "node -e \"fetch('http://127.0.0.1:5173').then(()=>process.exit(0)).catch(()=>process.exit(1))\" "
            "|| (pkill -f 'vite' 2>/dev/null || true; pkill -f 'npm run dev' 2>/dev/null || true; "
            "sleep 1; cd /app/frontend && (nohup npm run dev -- --host 0.0.0.0 --port 5173 --strictPort > /tmp/aidev-frontend.log 2>&1 &))",
            10000,
        )
        if frontend["exitCode"] == 0:
            outputs.append("frontend started")
        else:
            errors.append(frontend["stderr"] or frontend["stdout"] or "frontend start failed")

    return {
        "started": bool(outputs) and not errors,
        "outputs": outputs,
        "errors": errors,
        **(get_sandbox_info(sandbox_id) or {}),
    }

def restart_sandbox_preview(
    project_id: str,
    sandbox_id: str,
    user_id: str = "demo-user",
    preferred_backend_port: str | int | None = None,
    preferred_frontend_port: str | int | None = None,
) -> dict[str, Any]:
    stop_active_preview_for_user(user_id)
    info = _info(sandbox_id)
    if info:
        stop_sandbox_containers(sandbox_id)
        info.backend_container_id = None
        info.frontend_container_id = None
        info.db_container_id = None
    if not info or not info.backend_container_id or not info.frontend_container_id:
        if not reconnect_sandbox(sandbox_id, user_id, preferred_backend_port, preferred_frontend_port):
            return {"started": False, "errors": [f"Could not reconnect {sandbox_id}"]}
        info = _info(sandbox_id)
    elif info.user_id != user_id:
        info.user_id = user_id
    if info:
        _sandboxes[project_id] = info
        _active_preview_by_user[user_id] = sandbox_id
        schedule_preview_auto_stop(sandbox_id)
    return start_sandbox_servers(sandbox_id)
