# Phase 7 — Observability

*Per directive §17. Every component must produce structured, timestamped logs retrievable by Hermes for debugging, auditing, and compliance.*

---

## 7.1 Logging Architecture

**Three log layers:**
1. **Console output** — immediate, human-readable, attached to the running process. Used by Hermes when executing commands interactively.
2. **File logs** — persistent, structured (JSON Lines), timestamped, stored in known locations. Used by Hermes for post-hoc debugging and auditing.
3. **ClickUp tasks** — operational state tracking (task created, updated, completed). Used by Hermes Projects agent for status reporting.

---

## 7.2 Component Log Locations

| Component | Log Location | Format | Retention |
|-----------|-------------|--------|-----------|
| OpenClaw gateway | `C:\Users\HP\AppData\Local\Temp\openclaw\openclaw-YYYY-MM-DD.log` | JSON Lines (structured) | Perpetual (rotated daily) |
| OpenClaw gateway (manual start) | `C:\Users\HP\AppData\Local\Temp\openclaw\gateway-YYYYMMDD-HHMMSS.log` | stdout/stderr text | Perpetual |
| Hermes gateway | `C:\Users\HP\AppData\Local\hermes\logs\gateway.log` | Text | Perpetual |
| Hermes sessions | `C:\Users\HP\AppData\Local\hermes\sessions\sessions.json` | JSON | Perpetual |
| ClickUp wrapper (clickup.js) | Console stderr (`[clickup] METHOD /path -> STATUS`) | Text | Per-invocation (not persisted) |
| Proposal automation | Console output (stages) + evidence files | Text + Markdown | Perpetual (evidence/) |
| Gemini API calls | Console (invoke count, token usage in response) | Text | Per-invocation |
| Git operations | `git log` + GitHub commit history | Text | Perpetual |

---

## 7.3 Log Content Standards

**Every log entry must include:**
- **Timestamp:** ISO 8601 with timezone (e.g., `2026-09-05T00:35:11Z`)
- **Component identifier:** which system produced the log (e.g., `[clickup]`, `[gemini]`, `[openclaw]`)
- **Action:** what operation was attempted (e.g., `create-task`, `generateContent`, `sendMessage`)
- **Outcome:** success, failure, or warning with relevant detail
- **Identifiers:** relevant IDs (task ID, message ID, request ID, model name)

**Examples:**

```
[clickup] POST /list/1200430000004210/task -> 201  (taskId=123t3hvmy45)
[gemini] generateContent gemini-3.6-flash -> 200  (chars=4909, tokens=122)
[openclaw] telegram outbound send ok  (chatId=1360833951, messageId=41)
[proposal] STAGE 2: Gemini draft generated (4909 chars)
[proposal] STAGE 4: ClickUp task created: ID=123t3hvmy45
```

**Error entries must include:**
- Error type/code (e.g., `401`, `404`, `NotSupportedException`)
- Error message (redacted of secrets)
- Context (what operation was running, what inputs were used)
- Recommended action (if known)

**Example error entry:**
```
[clickup] POST /list/1200430000004210/task -> 401  (err="Unauthorized", ECODE="OAUTH_019")
[gemini] generateContent -> 400  (err="API key not valid", reason="API_KEY_INVALID")
[proposal] ERROR: Gemini API call failed: 400 Bad Request
```

---

## 7.4 Log Retrieval by Hermes

**Hermes can retrieve logs via:**

1. **Direct file read:** `read_file` on log paths (for recent entries)
2. **Terminal grep:** `grep` on log files for specific patterns (error codes, task IDs, timestamps)
3. **ClickUp API:** `clickup.js get-task <id>` to check task state
4. **OpenClaw Control UI:** `http://127.0.0.1:18789/` (browser-based, requires auth token)
5. **OpenClaw API:** direct HTTP calls to gateway endpoints (requires auth token)

**Log retrieval commands (Hermes-side):**

```bash
# Get latest 50 OpenClaw log entries
tail -50 "/c/Users/HP/AppData/Local/Temp/openclaw/openclaw-2026-09-05.log"

# Search for auth errors in OpenClaw logs
grep -i "auth\|401\|error" "/c/Users/HP/AppData/Local/Temp/openclaw/openclaw-2026-09-05.log"

# Get ClickUp task state
node clickup.js get-task <taskId>

# Get Hermes gateway status
hermes gateway status
```

---

## 7.5 Alerting

**Alert levels:**

| Level | Meaning | Action |
|-------|---------|--------|
| INFO | Operation succeeded | No action needed; logged for audit trail |
| WARN | Non-critical issue (retry succeeded, fallback used) | Logged; Hermes may note for awareness |
| ERROR | Operation failed, requires human attention | Logged + console error + (if applicable) Telegram notification + ClickUp task |

**Alert triggers:**

1. **Auth failure (401/403)** — any component: ERROR, alert human immediately
2. **Rate limit (429)** — any component: WARN, log retry attempt, alert if persistent
3. **Network timeout** — any component: WARN, retry with backoff, ERROR if all retries fail
4. **File write failure** — proposal automation: ERROR, abort operation
5. **Gateway crash** — OpenClaw: ERROR, alert human, scheduled task covers restart
6. **Gemini quota exceeded** — proposal automation: WARN, alert if recurring

---

## 7.6 Audit Trail

**Every operation that modifies state must leave an audit trail:**

1. **ClickUp task creation:** task ID, name, list, space, creator, timestamp — retrievable via ClickUp API
2. **Proposal generation:** draft file path, final file path, Gemini request ID, ClickUp task ID (if created) — stored in evidence/
3. **Telegram message:** message ID, chat ID, sender ID, timestamp, delivery status — logged in OpenClaw gateway log
4. **Git commit:** commit hash, message, author, timestamp, changed files — retrievable via `git log`
5. **OpenClaw model call:** provider, model, status code, latency, trace ID — logged in OpenClaw gateway log

**Audit retrieval:** All audit trails are retrievable by Hermes via the log retrieval methods above. No audit trail requires manual access to external systems beyond what Hermes can already reach.
