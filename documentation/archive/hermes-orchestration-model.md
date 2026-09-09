# HERMES DELIVERABLE 1 — Phase 2: Hermes Orchestration Model

**Date**: 2026-09-04  
**Status**: Phase 2 COMPLETE — routing model defined, delegation rules documented, decision tree specified

---

## 1. ORCHESTRATION ROLE

Hermes is the **sole orchestrator**. Every user request enters through Hermes (Telegram bot `8917859111:XXX` or CLI). Hermes decides:
- Whether the request can be handled directly with its built-in tools
- Whether it needs a specialist capability (Gemini for code, ClickUp for projects, OpenClaw for model fallback)
- Which tool/capability to use
- When to ask the user for input (human approval points — Phase 17)
- When to report back

Hermes does NOT delegate orchestration itself. It is the single decision point.

---

## 2. ROUTING DECISION TREE

For each incoming request, Hermes applies this decision tree:

```
INCOMING REQUEST
     │
     ▼
Can Hermes answer directly with built-in tools (web_search, web_extract, browser_exec, writing, analysis)?
     │
     ├── YES → Handle directly. No delegation.
     │
     ▼
Does the request involve CODE (writing, reviewing, debugging, explaining, generating)?
     │
     ├── YES → Route to Gemini (Gemini API / CLI). Delegate the code task.
     │           Return Gemini's output to user. Verify against user's intent.
     │
     ▼
Does the request involve PROJECTS / TASKS / ROADMAP (creating tasks, checking status, updating progress, viewing roadmap)?
     │
     ├── YES → Route to ClickUp (REST API via wrapper). Delegate the project task.
     │           Return results to user.
     │
     ▼
Is the request a GENERAL QUESTION or ANALYSIS where Solar Pro4 is the best model?
     │
     ├── YES → Use Hermes's primary model (Solar Pro4 via Nous Portal OAuth).
     │           This is the default path — no delegation needed.
     │
     ▼
Is the primary model (Nous Portal) unavailable, rate-limited, or producing errors?
     │
     ├── YES → Fall back to OpenClaw (Solar Pro4 via OpenRouter, gateway 127.0.0.1:18789).
     │           Route the request through OpenClaw's API/Telegram.
     │           Report to user: "Switching to fallback model path."
     │
     ▼
Does the request require USER DECISION / HUMAN JUDGMENT (pricing, client communication, destructive operations, scope changes)?
     │
     ├── YES → Do NOT execute. Present options to user. Wait for approval.
     │           (See Phase 17 — Human Approval Points)
     │
     ▼
DEFAULT → Handle with Hermes's primary model + built-in tools. No delegation.
```

---

## 3. DELEGATION RULES — WHEN TO USE EACH CAPABILITY

### 3.1 Hermes direct (built-in tools) — NO DELEGATION

Hermes uses its own tools when the request can be satisfied without a specialist model or external service:

| Capability | Hermes tools | When |
|---|---|---|
| Research / web search | `web_search`, `web_extract`, `browser_exec` | "Find competitors for X", "Research Y technology", "What's the latest on Z" |
| Reading / summarizing documents | `web_extract`, file reading | "Read this URL and summarize", "What does this doc say" |
| Web interaction / browsing | `browser_exec` | "Go to this site and click X", "Fill this form", "Verify this page works" |
| Writing / drafting | Built-in writing (Solar Pro4 via Nous Portal) | "Write a blog post", "Draft an email", "Create a proposal outline" |
| General analysis / Q&A | Built-in Solar Pro4 (Nous Portal) | "Explain this concept", "Compare X vs Y", "What's the best approach for Z" |
| Math / data analysis | Built-in reasoning | "Calculate this", "Analyze this data", "What does this number mean" |
| Planning / structuring | Built-in reasoning | "Plan this project", "Create a roadmap", "Break this down" |

**Rule**: If Hermes can do it with its own tools, do NOT delegate. Delegation adds latency, cost, and complexity.

### 3.2 Gemini (coding) — DELEGATE

Hermes delegates to Gemini when the request is code-related:

| Trigger | What to delegate | How |
|---|---|---|
| "Write code for X" | Generate code | Gemini API (or CLI in interactive mode if user wants to iterate) |
| "Review this code" | Code review | Gemini API with code as input |
| "Debug this error" | Debugging | Gemini API with error + code context |
| "Explain this code" | Explanation | Gemini API with code as input |
| "Convert this code from X to Y" | Refactoring/porting | Gemini API |
| "Generate a script for X" | Script generation | Gemini API |
| "What's the best library for X?" | Library recommendation | Gemini API |

**How Gemini is invoked:**
- **Primary**: Gemini REST API directly (curl/Node) — reliable, non-interactive, no headless hang.
- **Interactive**: Gemini CLI (`gemini --prompt "..." --skip-trust --model gemini-3.6-flash`) — when user wants to iterate in real time.
- **Model**: `gemini-3.6-flash` (the CLI/API default; `gemini-2.5-flash` is no longer available to new users per the API response).

**What Hermes does NOT delegate to Gemini:**
- Anything requiring human judgment (client comms, pricing, scope).
- Anything Hermes can do with its own tools (web search, writing, analysis).
- Anything requiring access to private user data Gemini doesn't have (Hermes must provide context).

### 3.3 ClickUp (projects) — DELEGATE

Hermes delegates to ClickUp when the request involves project management:

| Trigger | What to delegate | How |
|---|---|---|
| "Create a task in ClickUp" | Task creation | ClickUp REST API via wrapper script |
| "What's the status of project X?" | Status check | ClickUp REST API via wrapper script |
| "Update task Y to done" | Status update | ClickUp REST API via wrapper script |
| "Show me the roadmap" | List views / tasks | ClickUp REST API via wrapper script |
| "Add a subtask to X" | Subtask creation | ClickUp REST API via wrapper script |
| "Move task Y to sprint 2" | Status/iteration update | ClickUp REST API via wrapper script |

**How ClickUp is invoked:**
- ClickUp REST API (`https://api.clickup.com/api/v2/...`) via a wrapper script (Node or Python) that Hermes calls.
- Token: `CLICKUP_TOKEN` in Hermes `.env` (token starts `pk_240010007_...`, full value redacted for git).
- Wrapper script: **to be written in Phase 2/4**.

**What Hermes does NOT delegate to ClickUp:**
- Task content that needs Gemini's help (e.g., "write the task description for X" — use Gemini first, then store via ClickUp).
- Anything ClickUp can't do (ClickUp is only a task store; it doesn't generate content).

### 3.4 OpenClaw (model fallback) — USE ONLY WHEN PRIMARY FAILS

Hermes uses OpenClaw as a **fallback model path**, NOT as a primary routing step:

| Trigger | What to delegate | How |
|---|---|---|
| Hermes's primary model (Nous Portal OAuth) is unavailable | Route request through OpenClaw | Call OpenClaw API (gateway 127.0.0.1:18789) or send message to OpenClaw's Telegram bot |
| Primary model is rate-limited / returning errors | Route request through OpenClaw | Same as above |
| User explicitly asks to use OpenClaw's model | Route request through OpenClaw | Same as above |

**How OpenClaw is invoked:**
- OpenClaw API: `http://127.0.0.1:18789/` (gateway auth token required).
- OpenClaw Telegram: message `@bukomaopenclawbot` (bot token `8845344838:XXX`, allowlist 1360833951).
- OpenClaw's model: Solar Pro4 via OpenRouter (key verified live; model `openrouter/upstage/solar-pro4` in OpenClaw config).

**What Hermes does NOT delegate to OpenClaw:**
- Normal requests when the primary model is healthy — don't use fallback path unnecessarily.
- Requests that need Hermes's own tools (OpenClaw doesn't have Hermes's web_search, web_extract, browser_exec; it has its own toolset but not Hermes's).

### 3.5 GitHub / Git — DELEGATE WHEN CODE IS INVOLVED

Hermes delegates to GitHub/Git when the request involves repository operations:

| Trigger | What to delegate | How |
|---|---|---|
| "Create a repo for X" | `gh repo create` | `gh` CLI (authenticated as BukomaJumaMoya) |
| "Push this code to GitHub" | `git push` + `gh` | `git` + `gh` CLI |
| "Create a PR for X" | `gh pr create` | `gh` CLI |
| "Check issues for repo X" | `gh issue list` | `gh` CLI |
| "Clone repo X" | `git clone` | `git` CLI |
| "What's the branch status?" | `git branch` / `gh pr list` | `git` + `gh` CLI |

**How GitHub/Git is invoked:**
- `gh` CLI for GitHub API operations (authenticated via PAT).
- `git` CLI for local version control.
- Hermes writes code (via Gemini) → commits locally (git) → pushes to GitHub (gh).

---

## 4. WHAT HERMES DOES NOT DELEGATE

These are **never** delegated — Hermes handles them directly or asks the user:

| Category | Examples | Why |
|---|---|---|
| **Human judgment** | Pricing decisions, client communication, scope changes, contract terms | Requires human discretion; no tool can decide this |
| **Destructive operations** | Deleting repos, removing tasks, irreversible changes | Requires explicit human approval (Phase 17) |
| **Confidential user data without context** | Anything needing access to data Hermes doesn't have | Hermes must provide context to any delegated tool |
| **Primary model routing (normal path)** | "Answer this question" when Nous Portal is healthy | Use Hermes's own model; don't route through OpenClaw unnecessarily |
| **Tasks Hermes can do with built-in tools** | Web search, web extraction, browsing, writing, analysis | Delegation adds latency/cost/complexity without benefit |
| **Anything requiring credentials Hermes doesn't have** | API calls to services without configured tokens | Hermes must have the token before delegating |

---

## 5. ROUTING SUMMARY TABLE

| Request type | Primary path | Secondary/fallback | Delegate? |
|---|---|---|---|
| General Q&A / analysis | Hermes Solar Pro4 (Nous Portal) | OpenClaw Solar Pro4 (OpenRouter) | No (unless primary fails) |
| Research / web | Hermes tools (web_search, web_extract, browser_exec) | — | No |
| Writing / drafting | Hermes Solar Pro4 (Nous Portal) | OpenClaw Solar Pro4 (OpenRouter) | No (unless primary fails) |
| Code generation / review / debug | **Gemini API** (gemini-3.6-flash) | Gemini CLI (interactive) | **Yes** |
| Project / task management | **ClickUp REST API** (via wrapper) | — | **Yes** |
| Repository operations | **gh CLI + git** | — | **Yes** (when code involved) |
| Model fallback (primary down) | — | **OpenClaw → OpenRouter → Solar Pro4** | Route through OpenClaw |
| Human judgment / approval needed | Ask user | — | No execution until approved |

---

## 6. HUMAN APPROVAL POINTS (PRELIMINARY — FULL LIST IN PHASE 17)

Before executing any of these, Hermes must get explicit user approval:

- Sending messages to clients / external parties
- Creating repos / pushing code to GitHub (especially private repos)
- Creating / modifying / deleting ClickUp tasks (especially bulk operations)
- Running code that makes network calls, writes files, or modifies system state
- Any operation that costs money (API calls that aren't free, subscription usage)
- Any operation that can't be undone

---

## 7. ROUTING IMPLEMENTATION NOTES

- Hermes's routing is **prompt-based** — the routing logic lives in how Hermes is prompted (the system prompt + the per-request instructions). There is no separate routing daemon.
- The routing model above is the **policy** Hermes follows. It gets encoded into Hermes's system prompt and the prompt templates (Phase 11).
- OpenClaw routing is **not yet configured** — there's no automated failover from Hermes to OpenClaw. Currently, if Nous Portal fails, the user would need to ask Hermes to "use OpenClaw" or message `@bukomaopenclawbot` directly. Document this as a known limitation (Phase 19/23).
- OpenClaw → Hermes routing is **not configured** — they're independent runtimes. If the user messages `@bukomaopenclawbot`, OpenClaw answers directly; it doesn't forward to Hermes.

---

*Phase 2 complete. This routing model will be encoded into Hermes's prompt templates in Phase 11 and referenced in the end-to-end test (Phase 7).*
