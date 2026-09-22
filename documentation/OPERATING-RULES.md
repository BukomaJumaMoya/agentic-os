# Operating rules

The standing rules for any work on this repository or on the Hermes
installation it configures. Every prompt in this sequence says "follow
OPERATING-RULES.md"; this file is what that means.

## 1. Secrets

Never print, log, commit or echo a secret value. Key names and lengths only.

A key name (`GEMINI_API_KEY`), a length (`39 chars`) and a verdict (`valid`)
are reportable. A value, a prefix, a suffix, a bot ID or any fragment of a
token is not — not in a terminal, not in a log line, not in a commit, not in a
test fixture, not in a Telegram message, not in this documentation directory.

## 2. Archive, never delete

Archive, never delete, except files created in this session.

Superseded documents move to `documentation/archive/`. Superseded config is
backed up beside itself before it is rewritten. A file created during the
current session and found to be wrong may be removed outright, because nothing
is being destroyed that predates the session.

## 3. Evidence before moving on

Verify every change with evidence — command output, a log line, or a test —
before moving on. If verification fails, fix it before continuing.

"It should work" is not evidence. "The config now reads X" is not evidence that
the running system behaves as X: this repository already has one incident where
every config-reading diagnostic was correct while the surface on the wire was
wide open. Evidence means the observed behaviour of the thing itself.

## 4. Never loosen a control to make a test pass

Never loosen a security control to make a test pass. Fix the cause instead.

Widening an allowlist, relaxing a path check, deleting an assertion or
downgrading a failure to a warning in order to get a green run is prohibited.
If a control blocks a legitimate operation, the operation is wrong or the
control is wrong; establish which, in writing, before either moves.

## 5. Invariants

These five are invariants:

- the Telegram tool-surface allowlist
- the startup guard
- the approval patch
- per-agent `.env` isolation
- the coding sandbox confinement

Any change touching one of them must re-run the guard's negative tests. Not the
positive test — the negative ones, the cases that prove the guard still refuses.
A guard only verified in its passing direction is a comment.

## 6. The manifest is the surface

Every MCP tool is listed in `hermes/surface-manifest.json` with its owner.
Nothing reaches the Telegram surface unless it is in the manifest.

Adding a tool to an agent is therefore two edits, deliberately: the agent's
`tools.include` in Hermes' `config.yaml`, and the manifest. The startup guard
compares them on every start and refuses to start when they disagree.

## 7. When to stop and ask

Stop and ask Bukoma ONLY for:

- a browser or OAuth step
- a credential that has to be created by hand
- a real Telegram message that has to be sent from his phone
- an irreversible action outside this machine

Batch all such requests into a single message, then wait. Everything else is
decided and done without asking — including choices between reasonable
approaches, which are made, stated in the report, and moved past.

## 8. Finish with a report

Finish every prompt with a report: what changed, the evidence for it, and the
open items. Run all test suites. Then commit and push to master.

The report names what was *not* done as plainly as what was. A prompt finished
with a silently skipped item is not finished.
