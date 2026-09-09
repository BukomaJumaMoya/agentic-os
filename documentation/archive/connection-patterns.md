# HERMES DELIVERABLE 1 — Phase 6: Connection Patterns

**Date**: 2026-09-04  
**Status**: Phase 6 COMPLETE — connection patterns documented

---

## 1. OVERVIEW

This phase documents **how Hermes connects to each capability** — the technical patterns for invoking tools, passing data, handling responses, and managing errors. These are the concrete connection patterns that the end-to-end test (Phase 7) will exercise.

---

## 2. CONNECTION PATTERNS

### 2.1 Hermes → Web Search / Extract / Browse (built-in tools)

**Pattern**: Direct function call (Hermes's built-in tools).

**How**:
- Hermes calls `web_search(query, limit)` → returns URLs + snippets.
- Hermes calls `web_extract(urls, char_limit)` → returns markdown/text content.
- Hermes calls `browser_exec(code)` → runs browser automation.

**Data flow**:
```
User request ("research X")
     │
     ▼
Hermes: Decide this is research → use web_search
     │
     ▼
web_search("X", limit=5) → [url1, url2, url3, ...]
     │
     ▼
Hermes: Pick most relevant → web_extract(url, char_limit=15000)
     │
     ▼
web_extract → content (markdown)
     │
     ▼
Hermes: Synthesize with Solar Pro4 → answer to user
```

**Error handling**:
- `web_search` fails → try a different query or report to user.
- `web_extract` fails (page blocked, 404, etc.) → try another URL or report.
- Page too large for `web_extract` → truncated content + saved to disk; Hermes reads saved file if needed.

---

### 2.2 Hermes → Gemini (coding)

**Pattern**: REST API call (primary) or CLI invocation (interactive).

**API pattern (primary)**:
```
User request ("write a Python script that does X")
     │
     ▼
Hermes: Recognize code task → delegate to Gemini
     │
     ▼
Build API request:
  URL: https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}
  Body: {"contents": [{"parts": [{"text": "Write a Python script that does X. Requirements: ..."}]}]}
     │
     ▼
HTTP POST → Gemini API
     │
     ▼
Response: {"candidates": [{"content": {"parts": [{"text": "<code>..."}]}}]}
     │
     ▼
Hermes: Extract code text → present to user
```

**CLI pattern (interactive, user-initiated)**:
```
User: "spin up Gemini and help me write a script"
     │
     ▼
Hermes: Launch Gemini CLI
     │
     ▼
gemini --prompt "help me write a Python script that does X" --skip-trust --model gemini-3.6-flash
     │
     ▼
Gemini CLI → interactive session with user
```

**Data flow**:
- Hermes sends the task description + any relevant context (existing code, requirements, constraints) to Gemini.
- Gemini returns text (code, explanation, review, etc.).
- Hermes passes the result to the user.
- If code needs to be saved, Hermes writes files (Gemini API returns text, not files).

**Error handling**:
- API returns non-200 → report to user; try once more; if persistent, fall back to OpenClaw or report Gemini unavailable.
- API rate limit (429) → wait and retry; if persistent, report to user.
- CLI hangs (headless mode) → don't use CLI for automation; use API.
- Gemini returns empty/irrelevant output → refine prompt and retry once; if still bad, report to user.

---

### 2.3 Hermes → ClickUp (projects)

**Pattern**: REST API call via wrapper script (Node or Python).

**How the wrapper works**:
```
User request ("create a task in ClickUp for X")
     │
     ▼
Hermes: Recognize project task → delegate to ClickUp wrapper
     │
     ▼
Wrapper script (e.g. clickup.js or clickup.py):
  - Reads CLICKUP_TOKEN from environment
  - Builds API request:
    URL: https://api.clickup.com/api/v2/list/{list_id}/task
    Method: POST
    Headers: Authorization: {CLICKUP_TOKEN}
    Body: {"name": "X", "description": "..."}
     │
     ▼
HTTP POST → ClickUp API
     │
     ▼
Response: {"task": {"id": "...", "name": "X", ...}}
     │
     ▼
Wrapper: Returns task ID + details to Hermes
     │
     ▼
Hermes: Present result to user ("Task created: ID xyz, name X")
```

**Wrapper status**: Not yet written — to be implemented in Phase 15/16 (Automation).

**Data flow**:
- Hermes sends the task payload (title, description, status, etc.) to the wrapper.
- Wrapper calls ClickUp API.
- ClickUp returns task data.
- Wrapper returns task data to Hermes.
- Hermes presents to user.

**Error handling**:
- ClickUp API returns non-200 → wrapper returns error to Hermes; Hermes reports to user.
- Rate limit (429) → wrap waits and retries; if persistent, reports to user.
- Permission error (403) → report to user; may need different token or workspace access.
- Token missing → wrapper fails; Hermes reports "ClickUp not configured."

---

### 2.4 Hermes → GitHub / Git (repository operations)

**Pattern**: CLI invocation (`gh` + `git`).

**How**:
```
User request ("create a repo for X and push the code")
     │
     ▼
Hermes: Recognize repo operation → delegate to gh/git
     │
     ▼
Step 1: gh repo create X --private (or --public)
     │
     ▼
Step 2: git remote add origin git@github.com:BukomaJumaMoya/X.git (or https)
     │
     ▼
Step 3: git add . && git commit -m "initial"
     │
     ▼
Step 4: git push -u origin main
     │
     ▼
Result: repo created + code pushed
     │
     ▼
Hermes: Report to user
```

**Data flow**:
- Hermes constructs the `gh`/`git` commands based on the request.
- Hermes runs the commands (via its terminal tool or by instructing the user).
- `gh`/`git` return output.
- Hermes presents the result.

**Error handling**:
- `gh` auth fails → check `gh auth status`; if not logged in, need PAT re-application.
- Git fails ( Conflicts, auth, network) → report to user; fix and retry.
- Repo already exists → `gh` returns error; handle gracefully (offer to use existing repo).
- Push fails (no remote, wrong branch) → diagnose and report.

---

### 2.5 Hermes → OpenClaw (model fallback)

**Pattern**: API call to OpenClaw gateway OR message to OpenClaw Telegram bot.

**API pattern**:
```
Primary model (Nous Portal) unavailable
     │
     ▼
Hermes: Switch to fallback → OpenClaw
     │
     ▼
OpenClaw API: POST http://127.0.0.1:18789/...
  Auth: gateway auth token (c5e8c32e050a...663111)
  Body: {"message": "...", "model": "openrouter/upstage/solar-pro4"}
     │
     ▼
OpenClaw → OpenRouter → Solar Pro4 → response
     │
     ▼
Response → Hermes → user
```

**Telegram pattern (alternative)**:
```
Primary model unavailable
     │
     ▼
Hermes: Tell user "I'm switching to OpenClaw's model — message @bukomaopenclawbot"
     │
     ▼
User messages @bukomaopenclawbot
     │
     ▼
OpenClaw → OpenRouter → Solar Pro4 → response to user
```

**Data flow**:
- Hermes detects primary model failure (error, timeout, rate limit).
- Hermes routes the request through OpenClaw (via API or Telegram).
- OpenClaw uses its OpenRouter key to call Solar Pro4.
- Response comes back to Hermes (API) or directly to user (Telegram).

**Error handling**:
- OpenClaw gateway not running → report to user; start gateway or ask user to message `@bukomaopenclawbot` directly.
- OpenClaw OpenRouter key fails → report to user; key may need rotation.
- OpenClaw Telegram bot not responding → report to user.

**Current status**: OpenClaw → Hermes failover routing is **NOT configured**. If primary model fails, the user would need to manually message `@bukomaopenclawbot` or ask Hermes to use OpenClaw. This is a known limitation.

---

### 2.6 Hermes → Solar Pro4 via Nous Portal (primary model)

**Pattern**: Hermes's built-in model invocation (Nous Portal OAuth).

**How**:
```
User request ("explain X")
     │
     ▼
Hermes: Use primary model (Solar Pro4 via Nous Portal OAuth)
     │
     ▼
Hermes → Nous Portal → Solar Pro4 → response
     │
     ▼
Response → Hermes → user
```

**Data flow**:
- Hermes sends the prompt to its primary model (Nous Portal OAuth).
- Solar Pro4 returns a response.
- Hermes presents the response to the user.

**Error handling**:
- Model unavailable / rate limited → fall back to OpenClaw (Phase 2 routing).
- Timeout → retry once; if persistent, report to user.

---

## 3. AUTH LIGHTWEIGHT SUMMARY

| Connection | Auth mechanism | Where it's stored | Status |
|---|---|---|---|
| Hermes → Solar Pro4 (Nous Portal) | Nous Portal OAuth | Hermes config (OAuth session) | ✅ Working |
| Hermes → Gemini API | API key (`GEMINI_API_KEY`) | Hermes `.env` | ✅ Working (verified live) |
| Hermes → ClickUp API | Personal token (`CLICKUP_TOKEN`) | Hermes `.env` | ✅ Auth; wrapper not written |
| Hermes → GitHub (`gh`) | PAT (via `gh auth login --with-token`) | `gh` credential store | ✅ Working (verified: 14 repos) |
| Hermes → Git (local) | Local (user.name/email) | Git config | ✅ Working |
| Hermes → OpenClaw gateway | Gateway auth token | OpenClaw config (openclaw.json) | ⚠️ Gateway running; routing not configured |
| OpenClaw → OpenRouter | API key (in openclaw.json + state DB + agent DB) | OpenClaw stores | ✅ Working (verified: OpenRouter 200) |
| OpenClaw → Telegram | Bot token (`8845344838:XXX`) | OpenClaw config (openclaw.json) | ✅ Working (allowlist 1360833951) |
| User → Hermes Telegram | Bot token (`8917859111:XXX`) | Hermes `.env` | ✅ Working (allowlist 1360833951) |
| User → OpenClaw Telegram | Bot token (`8845344838:XXX`) | OpenClaw config (openclaw.json) | ✅ Working (allowlist 1360833951) |

---

*Phase 6 complete. Connection patterns documented for all capabilities. These patterns will be exercised in the end-to-end test (Phase 7).*
