# Documentation Status

**Last updated: 2026-09-09**

All documentation written before 2026-09-09 has been moved to
`documentation/archive/`. It is retained for history, not for reference, and it
should not be trusted. Many of those documents assert that work is complete,
verified, or passing — "29/29 items complete", "VERIFIED", "Status: PASS" — and
those claims are contradicted by facts since checked directly.

**Continuous integration has never passed.** Not once. The Actions history at
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

`AUDIT-agentic-os.md`, beside this file, supersedes everything in
`documentation/archive/`. Where the two disagree, the audit is correct.

## How to treat the archive

Treat every archived document as historical and unverified. Do not cite it as
evidence that something works, was tested, or was completed. To use a claim
from one, reconfirm it independently against the current code and the running
system first, and record what you found. Assume unreconfirmed claims are false.

## Debugging a silent gateway exit

Check `C:\Users\HP\AppData\Local\Temp\openclaw\openclaw-<date>.log` first. The
scheduled task launches the gateway with `--task-supervisor`, which discards its
stdout, so a failed start leaves nothing in the task result or `~/.openclaw/logs/`.
