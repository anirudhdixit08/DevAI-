from __future__ import annotations

import re

from ..models.contracts import AgentState
from ..services.gemini_client import safe_call_json_agent
from ..services.sandbox import read_file
from ._shared import apply_token_delta, increment_retry, log, reset_retry, retry_count, retry_limit


REVIEWER_PROMPT = """You are the Reviewer Agent in an AI software development team.

ROLE: Senior code reviewer. Last gate before code runs.

GOAL: Review for correctness and consistency. Approve or reject with actionable feedback.

REVIEW CHECKLIST:
1. IMPORTS: Do imports use EXACT importStatements from dependencies? Are relative paths correct? Is .js extension included?
2. EXPORTS: Does the file export what the interface says? Named vs default correct?
3. ASYNC/AWAIT: Are async functions called with await? No missing awaits on DB queries or API calls?
4. ERROR RESPONSE FORMAT: Does it use { success: true/false, data/message }? Consistent across all endpoints?
5. AUTH PATTERN: Uses "Bearer " prefix? Extracts with split(' ')[1]? Sets req.user after verify?
6. REQUEST/RESPONSE FIELDS: Do field names match between frontend API calls and backend route handlers?
7. ENV VARIABLES: Uses process.env.DATABASE_URL (not DB_URL)? Frontend uses import.meta.env.VITE_API_URL (not process.env)?
8. MIDDLEWARE ORDER: cors -> json -> routes -> error handler?
9. MODEL RETURNS: Do models return clean data (not raw { rows })? Does caller handle null/undefined?
10. SECURITY: Parameterized queries? No hardcoded secrets? Proper password hashing?
11. BACKEND LAYERING:
   - Reject route files that import models, pool/db, bcrypt/bcryptjs, jsonwebtoken, or contain SQL/business logic.
   - Reject route files with long inline async handlers instead of imported controller functions.
   - Reject controller files that create Router() or call router.get/post/put/delete.
   - Reject model files that use req/res or Express Router.
   - Reject duplicated endpoint logic split across both routes and controllers.
   - Reject imports from "bcrypt" when package.json only contains "bcryptjs"; use import bcrypt from "bcryptjs".
12. COMPLETENESS: Does it meet acceptance criteria?

OUTPUT FORMAT (strict JSON):
{
  "verdict": "approved" | "rejected",
  "issues": ["Specific issue 1", "Specific issue 2"],
  "summary": "One-line summary"
}

RULES:
- If approved, issues should be empty or minor suggestions.
- If rejected, issues MUST be specific and actionable - include exact line/code to fix.
- Be practical. Don't reject for style preferences - only bugs, security, missing functionality, or backend layering violations.
- If code is 90% correct with minor issues, APPROVE with suggestions.
- NEVER reject for missing features that are in a DIFFERENT task."""


def _layering_issues(files: list[dict], code_by_path: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for file in files:
        path = file.get("path", "")
        content = code_by_path.get(path, "")
        if not content:
            continue
        if "backend/src/routes/" in path:
            forbidden = []
            if re.search(r"from ['\"].*models/", content):
                forbidden.append("models")
            if re.search(r"from ['\"].*(config/db|db\.js)", content) or re.search(r"\b(pool|db)\.query\(", content):
                forbidden.append("database queries")
            if "bcrypt" in content:
                forbidden.append("bcrypt/bcryptjs")
            if "jsonwebtoken" in content or "jwt." in content:
                forbidden.append("JWT logic")
            inline_handlers = re.findall(r"router\.(get|post|put|patch|delete)\([^\n]+async\s*\(", content)
            if forbidden:
                issues.append(f"{path}: route file contains {', '.join(forbidden)}. Move business logic to controller/model and keep route as thin wiring.")
            if inline_handlers:
                issues.append(f"{path}: route file has inline async handlers. Import controller functions instead.")
        if "backend/src/" in path and re.search(r"from ['\"]bcrypt['\"]", content):
            package_json = code_by_path.get("backend/package.json", "")
            if '"bcryptjs"' in package_json and '"bcrypt"' not in package_json:
                issues.append(f'{path}: imports native bcrypt but package.json uses bcryptjs. Change the import to: import bcrypt from "bcryptjs";')
        if "backend/src/controllers/" in path:
            if "Router(" in content or re.search(r"router\.(get|post|put|patch|delete)\(", content):
                issues.append(f"{path}: controller file creates Express Router/routes. Move Router wiring to backend/src/routes and export handler functions only.")
        if "backend/src/models/" in path:
            if re.search(r"\b(req|res)\b", content) or "Router(" in content:
                issues.append(f"{path}: model file contains request/response or Router logic. Keep models limited to database functions.")
    return issues


async def reviewerAgentNode(state: AgentState) -> AgentState:
    current_cycle = state.reviewResult.get("reviewCycle", 0)
    files = (state.coderOutput or {}).get("files", [])
    if not state.currentTask or not files:
        if (state.coderOutput or {}).get("error"):
            state.reviewResult = {"verdict": "rejected", "issues": [state.coderOutput.get("notes", "Code generation failed")], "reviewCycle": current_cycle + 1}
            increment_retry(state, "reviewRejections", state.currentTask)
        else:
            state.reviewResult = {"verdict": "approved", "issues": [], "reviewCycle": 0}
            reset_retry(state, "reviewRejections", state.currentTask)
        return state

    code_content = ""
    code_by_path: dict[str, str] = {}
    backend_package_json = read_file(state.sandboxId, "backend/package.json")
    if backend_package_json:
        code_by_path["backend/package.json"] = backend_package_json
    for file in files:
        content = read_file(state.sandboxId, file.get("path", ""))
        if content:
            code_by_path[file.get("path", "")] = content
            code_content += f"\n--- {file.get('path')} ---\n{content}\n"

    task = state.currentTask
    user_prompt = f"TASK: {task.get('title')}\nDESCRIPTION: {task.get('description', '')}\n\n"
    user_prompt += "ACCEPTANCE CRITERIA:\n" + "\n".join(f"  - {item}" for item in (task.get("acceptanceCriteria") or [])) + "\n\n"
    user_prompt += f"CODE TO REVIEW:\n{code_content}\n"
    patterns = (state.contextPackage or {}).get("patterns") or {}
    if any(patterns.values()):
        user_prompt += "\nPROJECT PATTERNS (check compliance):\n"
        for key, value in patterns.items():
            if value:
                user_prompt += f"  {key}: {value}\n"

    result = await safe_call_json_agent(
        agent_name="reviewerAgent",
        system_prompt=REVIEWER_PROMPT,
        user_prompt=user_prompt,
        current_cost=state.tokenUsage.estimatedCost,
        token_budget=state.tokenBudget,
    )
    apply_token_delta(state, "reviewerAgent", result["tokens"])
    if not result["ok"]:
        state.error = f"reviewerAgent failed: {result['error']}"
        log(state, state.error)
        return state

    review = result["parsed"] or {}
    deterministic_issues = _layering_issues(files, code_by_path)
    if deterministic_issues:
        review["verdict"] = "rejected"
        review["issues"] = [*deterministic_issues, *(review.get("issues") or [])]
        review["summary"] = "Backend layering violations must be fixed before execution."
    state.reviewResult = {
        "verdict": review.get("verdict", "approved"),
        "issues": review.get("issues", []),
        "reviewCycle": current_cycle + 1,
        "summary": review.get("summary", ""),
    }
    if state.reviewResult["verdict"] == "rejected":
        attempts = increment_retry(state, "reviewRejections", state.currentTask)
        log(state, f"Reviewer rejection count: {attempts}/{retry_limit(state, 'reviewRejections', 2)}")
    else:
        reset_retry(state, "reviewRejections", state.currentTask)
    log(state, f"Reviewer verdict: {state.reviewResult['verdict']}")
    return state


def reviewerRouter(state: AgentState) -> str:
    if state.reviewResult.get("verdict") == "approved":
        return "executorAgent"
    attempts = retry_count(state, "reviewRejections", state.currentTask)
    max_attempts = retry_limit(state, "reviewRejections", 2)
    if attempts >= max_attempts:
        log(state, f"Reviewer retry limit reached ({attempts}/{max_attempts}); simplifying task")
        return "simplifyTask"
    return "contextBuilder"
