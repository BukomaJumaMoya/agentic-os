# PHASE 2 IMPLEMENTATION PLAN

**Status:** AUTHORITATIVE IMPLEMENTATION PLAN — supersedes earlier phase guidance where conflicts exist.
**Source decisions:** PHASE-2-DECISIONS.md (J11–J15), Phase 1 Reconnaissance Report, current constitution.

---

## 1. PHASING

### P1 — Foundation & Correctness (AUTHORIZED NOW)
### P2 — OpenClaw → Hermes Routing
### P3 — Specialist Agent Runtimes
### P4 — Proposal Quality & Config
### P5 — Reliability & CI
### P6 — Documentation
### P7 — Flagship End-to-End Flow
### P8 — Hardening & Observability

Each phase has explicit entry criteria and verification before the next is opened.

---

## 2. P1 — FOUNDATION & CORRECTNESS

### Objectives
- Fix the concrete correctness and security issues that block reliable agentic operation.
- Add minimal safety infrastructure so later phases can build safely.
- Preserve all working functionality; do not refactor beyond the fixes.

### Work Items

#### P1-1 Fix ClickUp command naming inconsistency
- Update README.md, documentation/stack.md, documentation/phase4-output.md, documentation/phase11-output.md, and any other docs to use `searchTasks` (actual code) or normalize the code to `search-tasks`. Preferred fix: keep code as-is and fix docs to `searchTasks`.
- Verify with live wrapper invocation.

#### P1-2 Fix proposal output path handling
- Make `generate-proposal.ps1` resolve `-OutputFile` deterministically relative to the script directory or repo root, and document it in README.
- Ensure evidence drafts and final output paths are unambiguous.

#### P1-3 Fix proposal placeholder leakage
- Update `generate-proposal.ps1` prompt to explicitly prohibit placeholders.
- Add post-generation check that the draft contains the exact client name and no obvious placeholders like `[Your Name]`.
- Optionally inject Juma details from `config/juma.json` when created in P4; for now use explicit prompt instructions only.

#### P1-4 Remove API key prefix logging
- Replace lines 65–68 in `generate-proposal.ps1` with `<SET>` / `<NOT SET>` logging.
- Confirm no other scripts log key prefixes.

#### P1-5 Reconcile completion-claim numbering
- Choose one numbering and apply consistently across README.md, DELIVERABLE-1-COMPLETION-REPORT.md, and phase11-output.md.
- Preferred: keep 26/26 for deliverables already delivered, and document 29/29 separately if items 27–29 are tracked elsewhere. Clarify rather than hide.

#### P1-6 Update stale specialist docs
- Update `documentation/projects-agent.md` to reflect that `automation/clickup.js` exists and is functional.
- Ensure `research-agent.md` and `coding-agent.md` do not contradict `phase4-output.md`.

#### P1-7 Populate or remove empty stubs
- Populate `automation/client-brief.md` with a short template/explanation of expected format, or remove it and update references.
- Populate `documentation/week-1-reflection.md` with actual reflection content, or remove it and update references.

#### P1-8 Add `.gitignore`
- Add `.gitignore` covering: `.env`, local config copies, `automation/evidence/*.md`, `node_modules/`, OS files, log files, `*.log`, `*.tmp`.

#### P1-9 Add minimal CI
- Add `.github/workflows/smoke.yml`:
  - `node --check automation/clickup.js`
  - PowerShell syntax validation for `automation/generate-proposal.ps1`
  - `node automation/clickup.js --help` smoke test
- Run on push to master.

### Entry Criteria
- Reconnaissance accepted; J11–J15 locked; PHASE-2-DECISIONS.md present; PHASE-2-IMPLEMENTATION-PLAN.md authored.
- No uncommitted secrets in tracked files.

### Exit Criteria / Verification
- All P1 work items complete and reviewed.
- CI workflow exists and passes on current repo state.
- `.gitignore` present.
- Wrapper command name consistent in code and docs.
- Proposal path deterministic and documented.
- No key-prefix logging.
- Completion-claim numbering consistent.
- Empty stubs either populated or removed.
- Stale agent docs updated.

---

## 3. P2–P8 SUMMARY

### P2 — OPENCLAW → HERMES ROUTING
- Implement custom OpenClaw tool that invokes `hermes chat -q` for forwarded messages.
- Disable Hermes direct Telegram bot or allowlist-disconnect it so OpenClaw is single entry point.
- Verify end-to-end Telegram → OpenClaw → Hermes → response.

### P3 — SPECIALIST AGENT RUNTIMES
- Create standalone agent runtimes:
  - `agents/research/main.py` (Python)
  - `agents/projects/main.js` (Node.js)
  - `agents/coding/main.py` (Python)
- Implement state machine, retries, and verification per J12/J13/J15.
- Hermes spawns agents as processes via terminal tool.

### P4 — PROPOSAL QUALITY & CONFIG
- Add `config/juma.json` with non-sensitive proposal defaults.
- Update proposal prompt to use config and prevent placeholders.
- Add content verification step.

### P5 — RELIABILITY & CI
- Add failure-oriented tests for 401/404/429/timeout/empty response.
- Implement retry/backoff in wrapper and proposal script matching documented policy.
- Add health check/watchdog for gateways.

### P6 — DOCUMENTATION
- Rewrite README as product README.
- Add runbook, troubleshooting guide, actual-architecture diagram.
- Consolidate agent docs; remove duplicates.

### P7 — FLAGSHIP END-TO-END FLOW
- Unstructured Telegram enquiry → classify → research → ClickUp records → proposal draft → verify → approval request.

### P8 — HARDENING & OBSERVABILITY
- Prompt injection policy enforcement.
- Credential rotation reminders.
- Observability improvements.

---

*Plan version: 2026-09-06. Awaiting per-phase authorization.*
