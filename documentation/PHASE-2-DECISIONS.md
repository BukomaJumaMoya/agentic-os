# PHASE 2 DECISIONS RECORD

**Date:** 2026-09-05
**Source:** Phase 1 Reconnaissance Report, Section J (J11–J15)

---

## Decided

### J11: OpenClaw → Hermes routing mechanism — APPROVED
Custom OpenClaw tool that calls `hermes chat -q` (one-shot query mode). Technical verification needed that `hermes chat -q` works reliably in non-interactive subprocess context.

### J12: Agent runtime model — APPROVED WITH OVERRIDE
**Original recommendation:** Hybrid (some as skills, some as processes)
**Override:** Separate processes only. Each specialist agent (Research, Projects, Coding) is its own standalone process. Hermes spawns them via its terminal tool. No hybrid, no skills-as-agents.

### J13: Agent language/runtime — APPROVED
Mixed, matching existing tooling:
- Projects agent: Node.js (leverages existing clickup.js)
- Coding agent: Python (Gemini API SDK, Hermes ecosystem alignment)
- Research agent: Python (same reasoning; verify web_search/web_extract callable standalone — fallback to Node.js if not)

### J14: Proposal prompt content source — APPROVED
Single non-sensitive config file at repo root (e.g., `config/juma.json`). Contains Juma's name, role, standard terms, contact info, default proposal parameters. `generate-proposal.ps1` reads it and injects into Gemini prompt.

### J15: Coding agent verification depth — APPROVED
Three-tier default:
1. Always: syntax check generated code (language-appropriate)
2. If tests exist or agent wrote tests: run them, report results
3. If code has safe trivial entry point: run it (e.g., --help, trivial input) if safe
Agent reports what it verified and what it didn't. No sandbox.

---

*End of Phase 2 Decisions Record.*
