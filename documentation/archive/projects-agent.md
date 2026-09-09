# HERMES DELIVERABLE 1 — Phase 4: Projects Agent Definition

**Date**: 2026-09-04  
**Status**: Phase 4 COMPLETE — projects capability defined; ClickUp wrapper to be implemented in Phase 15/16

---

## 1. ROLE

The Projects Agent is **not a separate process** — it's a **Hermes capability** that manages project tasks via ClickUp's REST API. Hermes invokes it when the request is project/task-related.

Projects handles:
- Creating tasks (with title, description, status, assignee, due date, priority, list/space)
- Reading task status / details
- Updating task status, description, due date, assignee
- Listing tasks (by status, by list, by assignee, by search)
- Creating / updating lists and spaces (where permissions allow)
- Managing roadmaps / views (where ClickUp's API supports it)

---

## 2. CONNECTOR

**ClickUp REST API** — `https://api.clickup.com/api/v2/...`

**Auth**: `CLICKUP_TOKEN` in Hermes `.env` (token starts `pk_240010007_...`, full value redacted for git; personal token, owner role in workspace "Juma Moya's Workspace", team ID 1200430000000602).

**Verified access**: curl `api.clickup.com/api/v2/team` → HTTP 200, workspace returned.

---

## 3. CAPABILITIES (WHAT THE WRAPPER SCRIPT DOES)

The ClickUp wrapper script (to be written in Phase 15/16) exposes these operations to Hermes:

| Operation | API endpoint | What Hermes passes |
|---|---|---|
| List tasks | `GET /team/{team_id}/task` | Optional: status, assignee, list_id, search query |
| Get task | `GET /task/{task_id}` | Task ID |
| Create task | `POST /list/{list_id}/task` | Title, description (optional), status (optional), priority (optional), due date (optional), assignee (optional) |
| Update task | `PUT /task/{task_id}` | Fields to update (status, description, title, due date, priority, assignee) |
| Delete task | `DELETE /task/{task_id}` | Task ID (requires human approval — destructive) |
| Create list | `POST /space/{space_id}/list` | Name, description (optional) |
| Get spaces | `GET /team/{team_id}/space` | — |
| Get lists | `GET /space/{space_id}/list` | — |

**Token storage**: The wrapper reads `CLICKUP_TOKEN` from the environment (Hermes `.env` is sourced before the wrapper runs).

**Error handling**: The wrapper returns ClickUp's HTTP status + error body to Hermes, which decides how to report it to the user.

---

## 4. OUTPUT FORMAT

Projects output should include:
- What was done (created / updated / listed / found)
- The relevant IDs (task IDs, list IDs) so Hermes can reference them later
- Any errors (ClickUp API errors, permission errors, rate limit errors)

---

## 5. LIMITS

- **API rate limits**: ClickUp's free tier has API rate limits (exact limits unknown; likely ~100 requests per 10 seconds). Bulk operations may hit limits.
- **Permissions**: The token has owner role in "Juma Moya's Workspace" — full access within that workspace. Can't access other workspaces without additional tokens.
- **Feature gaps**: ClickUp's API doesn't expose everything the UI does (e.g., some custom fields, automations, dashboards). The wrapper covers the core task/list/space operations; advanced features need separate investigation.
- **No CLI**: There is no maintained ClickUp CLI. Integration is API-only.

---

## 6. WHEN NOT TO USE PROJECTS

- Content creation (task descriptions, notes) → use Hermes direct or Gemini first, then store via ClickUp
- Research / web search → use Hermes research capability
- Code generation → use Gemini
- Anything ClickUp can't do → don't try; report to user

---

## 7. WRAPPER IMPLEMENTATION STATUS

- **API verified**: Yes (curl test, HTTP 200)
- **Token configured**: Yes (in Hermes `.env`)
- **Wrapper script written**: Yes — `automation/clickup.js` exists and is functional
- **Tested end-to-end**: Yes — verified create/read and search via wrapper

---

## 8. AGENT RUNTIME UPDATE (LOCKED)

Research, Projects, and Coding are no longer Hermes capabilities only.
They are now standalone separate processes:

- Research agent: Python
- Projects agent: Node.js
- Coding agent: Python

This is an implementation change for later phases.

---

*Phase 4 complete. Projects capability defined; ClickUp wrapper is the key deliverable for Phase 15/16.*
