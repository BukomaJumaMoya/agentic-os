# Phase 9 — Human-in-the-Loop Patterns

*Per directive §19. Every automation level must have an escape hatch, a visible status channel, and a clear owner.*

---

## 9.1 Human-in-the-Loop Design Principles

**Three principles govern all human-AI interaction in this stack:**

1. **No silent failures.** Every automation that fails must alert a human. No operation fails quietly in the background without notification.
2. **No irreversible actions without confirmation.** No operation that modifies external state (ClickUp tasks, files, git commits, API calls with side effects) proceeds without either explicit human confirmation or a clear audit trail that allows rollback.
3. **No black boxes.** Every automation stage logs what it did, what it got back, and what it's doing next. A human can inspect any stage's output at any time via the evidence folder or log retrieval methods in Phase 7.

---

## 9.2 Interaction Channels

### Channel 1: Telegram (Primary — Juma Moya ↔ Hermes/OpenClaw)

**When Hermes uses this channel:**
- Receive task requests from Juma Moya (e.g., "write a proposal for Acme Corp")
- Deliver progress updates during long-running operations
- Deliver error alerts when an automation fails
- Request human confirmation for irreversible actions
- Deliver final results (proposal text, task IDs, links)

**When OpenClaw uses this channel:**
- Reply to Juma Moya's direct questions (Solar Pro4 via OpenRouter)
- Confirm actions taken (e.g., "I created a ClickUp task: ...")
- Alert on auth failures or provider errors

**Human confirmation pattern (Telegram):**
```
Hermes: "I'm ready to create a ClickUp task for 'Acme Corp - Web App Proposal' in the Projects list. Confirm? (yes/no)"
Juma: "yes"
Hermes: "Task created: https://app.clickup.com/t/123t3hvmy45"
```

**Error alert pattern (Telegram):**
```
Hermes: "⚠️ Error: Gemini API returned 400 (API key not valid). Proposal not generated. Please check GEMINI_API_KEY in Hermes .env."
```

---

### Channel 2: Console / Terminal (Primary — Hermes executing commands)

**When Hermes uses this channel:**
- Execute ClickUp wrapper commands (`node clickup.js ...`)
- Execute proposal automation (`powershell ... generate-proposal.ps1 ...`)
- Read and write files (draft proposals, evidence, documentation)
- Run git operations (commit, push)

**Human visibility pattern (console):**
- Every command outputs its result directly to stdout/stderr
- Hermes reads the output before deciding next steps
- Errors are visible in the terminal output stream
- Hermes reports key outcomes back to the human via Telegram

**No separate "console log" is needed** — the terminal session itself is the log. Hermes's tool calls and their results are the audit trail for console operations.

---

### Channel 3: ClickUp Tasks (Secondary — status tracking)

**When ClickUp is used:**
- Track the state of a proposal (draft → ready → sent → won/lost)
- Track client-related tasks (onboarding, project milestones, invoices)
- Serve as the "source of truth" for what's in progress

**Human visibility pattern (ClickUp):**
- Hermes creates a task at the start of an operation
- Hermes updates the task status as the operation progresses
- Hermes adds comments to the task with key outcomes (proposal draft link, final proposal, client feedback)
- Juma Moya can view all tracked operations in the ClickUp Projects list at any time

**Task lifecycle:**
```
[to do] → [in progress] → [complete]
     ↓           ↓              ↓
 Created    Draft ready    Proposal sent
            (evidence saved)
```

---

### Channel 4: Files / Evidence (Tertiary — audit trail)

**When files are used:**
- Store proposal drafts before final delivery
- Store final proposals for client delivery
- Store automation outputs for debugging and audit
- Store configuration state for reproducibility

**Evidence folder structure:**
```
evidence/
├── proposal-draft-Acme-Corp-2026-09-05T00-35-11Z.md   # Gemini draft
├── proposal-draft-Beta-Industries-2026-09-05T00-36-43Z.md
├── proposal-test-acme.md                                  # Final proposal (test)
├── proposal-test-beta.md                                  # Final proposal (test)
└── ... (future: client briefs, research notes, etc.)
```

**Human visibility pattern (files):**
- Every file is named with a clear pattern: `<type>-<client>-<timestamp>.md`
- Files are in a known location (`evidence/`) that Hermes can read at any time
- Files persist across sessions — a human can inspect them later without Hermes

---

## 9.3 Automation Levels and Escape Hatches

### Level 0: Manual (no automation)
- **What:** Human does everything via ClickUp UI, Gemini web console, file editor
- **Escape hatch:** N/A (human is in full control)
- **Status channel:** ClickUp UI, file system
- **Use when:** First time doing a task, high-stakes client, unusual requirements

### Level 1: Assisted (Hermes executes individual steps)
- **What:** Hermes runs individual commands on human request (e.g., "create a ClickUp task", "generate a proposal draft")
- **Escape hatch:** Human can abort at any step by not confirming the next step
- **Status channel:** Console output + Telegram confirmation per step
- **Use when:** Standard tasks with familiar patterns, moderate stakes

### Level 2: Orchestrated (Hermes runs full pipeline)
- **What:** Hermes runs `generate-proposal.ps1` end-to-end (Gemini → ClickUp → file) in one invocation
- **Escape hatch:** Script can be interrupted; each stage logs independently; evidence files are saved even if script is interrupted
- **Status channel:** Console output (stages) + Telegram summary at end + ClickUp task created
- **Use when:** Standard proposal with known client and service, low-to-moderate stakes

### Level 3: Scheduled (future — not yet implemented)
- **What:** Automation runs on a schedule (e.g., weekly proposal review, daily status update)
- **Escape hatch:** Schedule can be paused/disabled; each run produces evidence files
- **Status channel:** ClickUp task + file evidence + (future) Telegram digest
- **Use when:** Routine recurring tasks, low stakes

**Current deployment:** Level 1 and Level 2 are operational. Level 3 is not yet implemented (no scheduler configured beyond OpenClaw's built-in cron, which is available but not yet used for freelance stack tasks).

---

## 9.4 Confirmation Requirements

**Operations that require explicit human confirmation before execution:**

| Operation | Confirmation Required? | Channel |
|-----------|----------------------|---------|
| Create ClickUp task | Yes (unless `--yes` flag or Level 2+) | Telegram / Console |
| Update ClickUp task status | Yes (for status changes that imply progress) | Telegram / Console |
| Generate proposal with real client name | Yes (confirm client name and service) | Telegram / Console |
| Send proposal to client (email/WhatsApp) | Yes (always — external communication) | Telegram |
| Git commit with client data | No (version control is safe; can revert) | Console |
| Git push to remote | No (pushes are public but reversible via force push if needed) | Console |
| Delete a ClickUp task | Yes (destructive operation) | Telegram / Console |
| Rotate an API key | Yes (requires recreating key at provider) | Telegram |

**Operations that do NOT require confirmation (safe to run automatically):**

| Operation | Why Safe |
|-----------|----------|
| Read ClickUp tasks | Read-only, no side effects |
| Read Gemini API (test call) | Read-only, no side effects (generates minimal token usage) |
| Write proposal draft to evidence/ | Internal file, no external impact, can be deleted |
| Create evidence/ directory | Idempotent, no side effects |
| Git status / diff / log | Read-only, no side effects |

---

## 9.5 Error Escalation Path

**When an automation fails, the escalation path is:**

1. **Operation fails** → error logged at ERROR level (see Phase 6 — Error Handling)
2. **Retry attempt** → if error is transient, retry once (see Phase 6 retry policies)
3. **Retry fails or error is permanent** → alert human via the appropriate channel:
   - If Hermes was interacting via Telegram: send Telegram alert to Juma Moya (ID 1360833951)
   - If Hermes was executing via console: output ERROR to console (Hermes reads it and can report via Telegram)
   - If operation had a ClickUp task: add error comment to the task
4. **Human acknowledges** → human can:
   - Fix the issue (e.g., update API key) and retry
   - Skip the failing step and continue (if safe)
   - Abort the operation entirely
5. **Escalation timeout** → if human doesn't respond within a defined window (e.g., 24 hours for non-urgent issues), the operation is marked as stalled in ClickUp (status = "blocked") and no further retry attempts are made until human intervenes.

**No operation retries indefinitely.** Every retry policy has a maximum (see Phase 6). After max retries, the operation stops and escalates to human.

---

## 9.6 State Handoff Between Human and AI

**When handing off state from AI to human (AI completes work):**
1. AI writes outputs to known locations (evidence/, ClickUp task, file)
2. AI reports summary via Telegram (what was done, where outputs are, any next steps)
3. AI does NOT take further action without human direction (unless Level 3 scheduled automation)

**When handing off state from human to AI (human requests work):**
1. Human provides request via Telegram or console
2. AI confirms understanding (restates request, asks for missing details if any)
3. Human confirms (or AI proceeds if request is clear and low-risk)
4. AI executes and reports results

**No state is lost between sessions.** All outputs are in files or ClickUp tasks. Hermes can pick up where it left off by reading the evidence folder and ClickUp task state at the start of any session.
