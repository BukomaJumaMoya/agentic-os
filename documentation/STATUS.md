# Documentation Status

**Last updated: 2026-09-09**

All documentation written before 2026-09-09 has been moved to
`documentation/archive/`. It is retained for history, not for reference, and it
should not be trusted. Many of those documents assert that work is complete,
verified, or passing — "29/29 items complete", "VERIFIED", "Status: PASS" — and
those claims are contradicted by facts since checked directly.

**Continuous integration passes as of 2026-09-23** — run 35826159192, both
`quality` and `smoke` green. It had never passed before that, on any commit,
for four separate reasons that were fixed together: the credential scanner
flagged the redaction test's own synthetic vectors; every test hardcoded the
Windows venv path; the agents refused to start because `.env` files are
gitignored and CI had none; and the confinement tests encoded Windows path
literals that are meaningless on Linux. See `documentation/AUDIT-2.md`.

The paragraph below describes the situation up to that date and is kept
because the archived reports it refers to still cite CI as evidence.

**Continuous integration had never passed.** Not once. The Actions history at
https://github.com/BukomaJumaMoya/agentic-os/actions shows every workflow run
ending in failure. The `quality` workflow fails on its first step, so every
test step after it — approval-boundary, flagship, integration — has been
skipped in every run. No automated check has ever validated this repository.
Archived reports citing CI as evidence of correctness cite something that did
not happen.

**The running system does not match the documented architecture.** The live
deployment reaches the agent through a path most archived reports do not
describe at all. Documents reasoning about the documented path are reasoning
about a system that is not the one in operation.

**Security problems were present throughout the period those reports were
written**, not introduced afterwards. Reports describing the system as
hardened or complete were describing a system that already had unresolved
issues in the approval boundary, the local HTTP router, and the agent
file-write and command-execution paths.

## Current source of truth

In this order, most current first. Where any two disagree, the higher one wins.

| document | what it is for |
|---|---|
| `../README.md` | the architecture as built, and the open items |
| `RUNBOOK.md` | how to operate it: daily use, updates, rotation, quota |
| `AUDIT-2.md` | the second adversarial pass — every claim with its evidence |
| `E2E-RESULTS.md` | the live end-to-end run, graded from logs |
| `OPERATING-RULES.md` | the standing rules the work was done under |
| `MCP-SERVERS.md` | every third-party server, its pin, licence and allowlist |
| `AUDIT-agentic-os.md` | the **first** audit, of the pre-Hermes system. Its findings drove this work; `AUDIT-2.md` supersedes its verdicts |
| `archive/` | history. Not reference. See below |

Everything in `archive/` is superseded by all of the above.

## How to treat the archive

Treat every archived document as historical and unverified. Do not cite it as
evidence that something works, was tested, or was completed. To use a claim
from one, reconfirm it independently against the current code and the running
system first, and record what you found. Assume unreconfirmed claims are false.

## Follow-up: `external_action.py` sends are still on the dead route

`external_action.py`'s `send_proposal` and `send_message` branches still POST
`{to, text, approval_request_id}` to `/message/send`, which does not exist —
the gateway has no REST message route and returns 404. They are unreachable
from any production path: nothing wires `execute_external_action` to a live
caller (`flagship.py` never calls it; only `resume.py` and a stdin `main()`
do). Three tests in `test_step7_flagship_e2e.py` do call it behind a `patch()`
of `_post`, but the file's `main()` runner never invokes those three, so under
the normal script runner they do not execute at all — a passing suite is not
evidence this path works, and neither is the presence of those tests.

Follow-up: migrate both branches to the same
`conversations_list` / `conversations_send` flow used by
`telegram_approval.send_approval_prompt`, and update the tests' patch targets
(`external_action._post`, `orchestrator.telegram_approval._post`) at the same
time — under pytest, which does collect them, they would otherwise start
making real, failing network calls.

## Gateway startup: `--task-supervisor` removed

`C:\Users\HP\.openclaw\gateway.cmd` no longer passes `--task-supervisor`.
That flag routes startup through OpenClaw's Windows job-anchor path, which
**fails to bind port 18789 for a still-unidentified reason inside its
FFI/`CreateProcessW` layer**: the gateway child exits 0 before its logging
subsystem initialises, so it produces no output anywhere. Ruled out by
testing: argument quoting, the environment block, the `koffi` native
dependency, port/lock contention, log capture, and stdio completion
signalling (`stdio[1]` patched to `"pipe"` — no effect, reverted). Running
without the flag has bound reliably in every test, so it is the current
stable approach; Task Scheduler's own `RestartCount`/`RestartInterval`
policy is the supervision layer instead of the job object.

Caveat: `dist/schtasks-Cnaz4sRo.js:258` re-appends the flag unconditionally
whenever the launcher is regenerated, so `openclaw gateway install` or an
update will silently undo this. Re-check `gateway.cmd` after either.

When a startup does fail silently, check
`C:\Users\HP\AppData\Local\Temp\openclaw\openclaw-<date>.log` first — it is
separate from `~/.openclaw/logs/` and is where the gateway's own diagnostics
land.
