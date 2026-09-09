# Phase 6 — Error Handling

*Per directive §16. Every component must have defined error modes, retry logic, alerts, and fallback paths.*

---

## 6.1 ClickUp API (clickup.js)

**Error modes:**
- `401 Unauthorized` — token expired or invalid. **Alert:** console error with status. **Retry:** no (token must be refreshed). **Fallback:** none — requires human to update CLICKUP_TOKEN.
- `404 Not Found` — space/list/task doesn't exist or token lacks access. **Alert:** console error. **Retry:** no. **Fallback:** verify space/list IDs and token permissions.
- `429 Rate Limited` — ClickUp rate limit hit. **Alert:** console warn. **Retry:** exponential backoff, max 3 retries, 2s/4s/8s delays. **Fallback:** queue operation for later.
- Network timeout (15s) — ClickUp API unreachable. **Alert:** console error. **Retry:** 3 retries with backoff. **Fallback:** fail with error message; Hermes can retry later.
- Invalid JSON response — ClickUp returned malformed data. **Alert:** console error with raw response. **Retry:** no. **Fallback:** fail; report raw response for debugging.

**Retry policy:** All transient failures (network timeout, 429) retry 3 times with 2s/4s/8s backoff. Auth failures (401) and not-found (404) never retry automatically.

---

## 6.2 Gemini API (generate-proposal.ps1)

**Error modes:**
- `400 INVALID_ARGUMENT` — bad request (malformed prompt, invalid key). **Alert:** Write-Error with exception message. **Retry:** no. **Fallback:** fail — Gemini key or prompt needs fixing.
- `403 PERMISSION_DENIED` — key lacks generative content permission. **Alert:** Write-Error. **Retry:** no. **Fallback:** regenerate/rotate key at aistudio.google.com.
- `429 RESOURCE_EXHAUSTED` — quota exceeded. **Alert:** Write-Warning. **Retry:** 1 retry after 30s. **Fallback:** queue for later; notify human if quota is consistently exhausted.
- Network timeout (60s) — Gemini API unreachable. **Alert:** Write-Error. **Retry:** 1 retry after 10s. **Fallback:** fail; Hermes can retry later.
- Empty/missing candidates — Gemini returned no response (safety filter, model issue). **Alert:** Write-Warning. **Retry:** 1 retry with reworded prompt. **Fallback:** use placeholder text; notify human.

**Retry policy:** 1 retry for quota (429) with 30s delay; 1 retry for network timeout with 10s delay; 1 retry for empty response with reworded prompt. Auth/permission failures never retry.

---

## 6.3 OpenClaw Gateway (Telegram → Solar Pro4 → Telegram)

**Error modes:**
- `401 Authentication failed` — OpenRouter key invalid/expired. **Alert:** log with `logLevelName: ERROR`, Telegram bot notifies user with "⚠️ Authentication failed..." message. **Retry:** not automatic (key must be refreshed). **Fallback:** bot tells user to re-authenticate via OpenClaw dashboard.
- `400 Bad Request` — model ID wrong or prompt too long. **Alert:** log ERROR. **Retry:** no. **Fallback:** bot notifies user; Hermes can fix model config.
- Telegram API error (send fails) — bot can't deliver reply. **Alert:** log WARN. **Retry:** 1 retry after 5s. **Fallback:** log failure; user can retry by sending another message.
- Gateway crash/port closed — node process dies. **Fallback:** Windows scheduled task "OpenClaw Gateway" restarts on next logon; manual restart via `node .../dist/index.js gateway --port 18789`.
- Memory pressure — node process uses too much memory. **Alert:** not currently monitored. **Fallback:** process may be killed by OS; scheduled task covers restart.

**Retry policy:** Telegram send retries once after 5s. Model fetch (OpenRouter) does not auto-retry on 401/400 — requires human intervention. Gateway process has no auto-restart beyond scheduled task.

---

## 6.4 GitHub CLI (gh)

**Error modes:**
- `auth failed` — token expired or revoked. **Alert:** gh outputs auth error. **Retry:** no. **Fallback:** re-authenticate with `gh auth login --with-token`.
- Network timeout — GitHub API unreachable. **Alert:** gh outputs error. **Retry:** gh has built-in retry. **Fallback:** retry manually.
- Rate limit (5000 req/hr for PAT) — hit limit. **Alert:** gh outputs 403 rate limit. **Retry:** wait and retry. **Fallback:** use higher-rate-limit token if available.

---

## 6.5 File System Operations (generate-proposal.ps1)

**Error modes:**
- Path not supported (colons in path) — Windows doesn't allow `:` in filenames. **Alert:** Out-File throws NotSupportedException. **Retry:** no. **Fallback:** sanitize timestamp format (removed colons, use `-` instead).
- Directory not found — evidence/ doesn't exist. **Alert:** none (auto-created). **Retry:** no. **Fallback:** `New-Item -ItemType Directory -Force` creates it.
- Permission denied — can't write to evidence/ or output path. **Alert:** Out-File throws. **Retry:** no. **Fallback:** fail; human must fix permissions.
- Disk full — can't write file. **Alert:** Out-File throws. **Retry:** no. **Fallback:** fail; human must free space.

---

## 6.6 Common Recovery Patterns

**All components follow the same recovery pattern:**
1. Error detected → logged with level (ERROR/WARN/INFO) and context
2. Retry decision based on error type (transient vs permanent)
3. If retry succeeds → log INFO, continue
4. If retry fails or error is permanent → alert human (console error / Telegram notification / ClickUp task)
5. Fallback path executed if defined (e.g., skip ClickUp, use placeholder text)
6. Operation state saved to evidence/ for later inspection

**Human alert channels (in order of preference):**
1. Console output (for automated runs observed by Hermes)
2. Telegram notification (for runs triggered by user via bot)
3. ClickUp task with error label (for async/long-running operations)
