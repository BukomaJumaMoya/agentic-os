# agentic-os

An orchestrated assistant for a one-person software consultancy. Requests arrive
over Telegram; a router decides which specialist handles them; the specialist
does the work in its own OS process, with its own credentials and its own
confinement.

The router holds no capability of its own — no terminal, no filesystem, no code
execution, no browser. Every boundary in this system is enforced in code and
covered by a test that proves it still refuses.

Built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.21.3.

---

## Architecture

```
  Telegram ──▶ Hermes gateway ──┬── clarify · memory · session_search
                                │   (3 built-in tools; nothing else)
                                │
                                ├─ stdio MCP ─▶ research      research
                                ├─ stdio MCP ─▶ pm            pm_query · pm_action*
                                ├─ stdio MCP ─▶ coding_agent  start_code_task* · get_status
                                │                             get_result · list_changed_files
                                │                             github_query · github_action*
                                │                             docs_query
                                ├─ stdio MCP ─▶ docs_agent    draft_proposal
                                ├─ stdio MCP ─▶ n8n_agent     list_workflows · run_workflow*
                                └─ stdio MCP ─▶ kola_agent    kola_catalogue · kola_query

                                    * requires human approval before the call is sent
```

| agent | scope | holds |
|---|---|---|
| **research** | Tavily web research, read-only | no write tool in the process at all |
| **pm** | ClickUp tasks and spaces | no delete |
| **coding_agent** | Pi in a Docker sandbox; GitHub and Context7 as MCP clients | cannot touch its own checkout |
| **docs_agent** | client proposals, rendered to PDF | no SMTP, no ClickUp, no bot token |
| **n8n_agent** | named local workflows, by webhook | not the n8n admin key |
| **kola_agent** | Kolaborate marketplace, read-only | inner-operation allowlist |

**Third-party MCP servers attach to an agent, never to the router.** GitHub's
server (32 tools), Context7 and Kolaborate are connected by an agent acting as
an MCP client; the router sees only that agent's own tools. This keeps the
router's prompt small — it is metered against a daily token cap — and puts
policy between a third-party tool and the thing that decides to call it.

---

## Security model

**Writes require a human.** Four tools change something outside this machine:
`pm_action`, `start_code_task`, `github_action`, `run_workflow`. Each blocks
inside the call until a person answers a Telegram card offering **Allow Once**
or **Deny**. The gate is Hermes' own (`_trust_gate_check`), armed by
`trust: untrusted` and aimed by the `readOnlyHint` annotation each agent
publishes. The model is not consulted and cannot skip it. An approval is
consumed by the call it authorises, so a replayed approval resolves nothing.

**A startup guard, eight conditions, fail-closed.** The gateway refuses to start
if the live configuration drifts from `hermes/surface-manifest.json` — the
single declaration of which tool belongs to which agent. It covers both
unattended surfaces (`telegram` and `cron`), and a refusal is announced on three
channels: Telegram, the Windows Event Log and a file.

**One credential per file.** Every agent has its own gitignored `.env`. Nothing
is shared except the model-provider keys, which are duplicated per agent on
purpose — a shared file would defeat the isolation that stops the research agent
reaching a ClickUp token.

**Untrusted text is fenced.** Everything an agent returns to a model is wrapped
with a per-call random delimiter, so injected text cannot close the block and
speak as the system.

Full evidence for each claim: [`documentation/AUDIT-2.md`](documentation/AUDIT-2.md).

---

## Install

Requires Python 3.11+, Node 20+, Docker, and a Hermes Agent installation.
Tested on Windows 11; the agents and tests also run on Linux in CI.

```bash
python -m venv agents/.venv
agents/.venv/Scripts/pip install -r requirements.txt

docker build -t juma-pi-sandbox:2 agents/coding     # coding sandbox
(cd agents/coding/vendor && npm ci)                 # Pi, from the committed lockfile

python hermes/post_update.py                        # configure, patch, and prove it
hermes gateway restart
```

`post_update.py` is the only setup command that matters. It applies the MCP
configuration, three patches to Hermes, the model chain and the launcher — then
runs all eight guard conditions and the 140 negative tests that prove the guard
still refuses. **Anything but `PASS` means do not start the gateway.**

Run it again after every `hermes update`: Hermes is a git checkout and `update`
stashes local changes, so every patch is one update away from silently not
existing.

### Credentials

Each file holds only what that agent needs. Names only:

| file | keys |
|---|---|
| `%LOCALAPPDATA%\hermes\.env` | `GEMINI_API_KEY` `OPENROUTER_API_KEY` `GROQ_API_KEY` `TELEGRAM_BOT_TOKEN` `TELEGRAM_ALLOWED_USERS` |
| `agents/research/.env` | `TAVILY_API_KEY` + model chain |
| `agents/pm/.env` | `CLICKUP_TOKEN` `CLICKUP_TEAM_ID` + model chain |
| `agents/coding/.env` | `GITHUB_TOKEN` `CODING_ROOT` `CODING_SANDBOX` + model chain |
| `agents/docs/.env` | model chain only |
| `agents/kola/.env` | `KOLA_API_KEY` |
| `agents/n8n/.env` | `N8N_API_KEY` `N8N_WEBHOOK_SECRET` |

```bash
python hermes/check_keys.py     # one authenticated call per key; names and verdicts, never values
```

---

## Usage

One Telegram chat. Reads answer immediately; writes ask first.

| you want | say |
|---|---|
| workspace questions | *"What's overdue?"* |
| web research | *"Research X"* — returns a cited summary |
| library documentation | *"What does the httpx AsyncClient timeout do? Check the docs."* |
| repository questions | *"In owner/repo, what are the last 3 commits?"* |
| a client proposal | `proposal: <the enquiry, pasted>` |
| a coding task | `code: <ClickUp task id or a description>` |
| create a task / open a PR | plain English → **approval card** |

Scheduled, unattended, and silent when there is nothing to say:

| job | when |
|---|---|
| daily briefing — overdue and due today | 07:30, Mon–Fri |
| weekly review — completed, slipped, stale PRs | 08:00, Monday |
| n8n notice drain | every minute |

Send **`/new`** when you switch to an unrelated task. Tool routing is reliable
in a fresh session and degrades past roughly 70,000 tokens of history; this is
the one operating habit worth keeping, and `## Status` explains why.

**Deliverables go to the operator. Nothing in this system can send anything to a
client** — the docs agent has no SMTP, no ClickUp token and no bot token.

---

## Operations

```bash
python hermes/post_update.py         # after every hermes update — 9 steps, all must PASS
python hermes/check_keys.py              # every credential, by name and verdict
python hermes/scan_stale_credentials.py  # credential copies nothing is tracking
python hermes/verify_agents.py           # all six agents boot and match the manifest
python hermes/redact_docs.py --check     # nothing leaked into documentation/
```

Day-to-day operation, quota management, key rotation and what to do when the
guard refuses: [`documentation/RUNBOOK.md`](documentation/RUNBOOK.md).

### Capacity

Free tiers bind, and the numbers are measured, not estimated:

| workflow | input tokens | runs/day |
|---|---|---|
| a simple read | 13,200 | ~19 |
| daily briefing | 16,625 | ~15 |
| proposal | ~50,000 | ~5 |
| coding task | ~100,000 | ~2 |

They share one budget: Gemini 250,000 input tokens/day per model, two models in
the chain, then OpenRouter's 50 requests/day account-wide as the last resort.

---

## Tests

```bash
agents/.venv/Scripts/python tests/test_surface_guard.py     # 140 assertions
```

398 assertions across seven files, run against real MCP stdio clients rather
than mocks. The suite is weighted toward **negative** tests — proving each
boundary still refuses when broken — because a check that cannot fail is not a
check. CI runs `quality` and `smoke` on every push.

---

## Layout

```
agents/_common/      shared scaffold: env scrubbing, structured errors, audit log,
                     provider chain, MCP client, untrusted-content fencing,
                     path confinement
agents/<name>/       one agent per directory, one .env each
hermes/              configuration, patches and checks for the Hermes checkout;
                     surface-manifest.json is the tool allowlist
tests/               real stdio clients, negative tests first
documentation/       audit, runbook, final report, operating rules
archive/pre-hermes/  the previous pipeline, retained as history
```

---

## Status

Production, in daily use, with known limitations recorded rather than smoothed
over.

| open item | detail |
|---|---|
| **read-path routing degrades in a long session** | in a fresh session `docs_query` and `github_query` are called correctly every time. Past ~70k tokens the model answers from memory instead, or claims a capability it has. Proactive pruning does not fix it: that code path only runs *after* a tool call, and the failure is that tool calls stop. `/new` is the free workaround; `compression.threshold_tokens` is the fix. See the final report |
| n8n has no live workflow | by choice — driving one would mean a second copy of the ClickUp credential |
| injection carriers | fencing verified at the function level, not through a ClickUp description, PR body or n8n payload |
| sandbox base image | `FROM node:22-bookworm-slim` is a tag. Everything installed *into* the image is pinned exactly; the base is not |
| disclosed identifiers | the bot token and chat ID are in this repository's history. Redaction stops new exposure; it does not undo the old |

- [`documentation/FINAL-REPORT.md`](documentation/FINAL-REPORT.md) — what was built, every audit verdict, what to fix next
- [`documentation/AUDIT-2.md`](documentation/AUDIT-2.md) — sixteen adversarial items with evidence
- [`documentation/E2E-RESULTS.md`](documentation/E2E-RESULTS.md) — live end-to-end runs, graded from logs
- [`documentation/RUNBOOK.md`](documentation/RUNBOOK.md) — operating it
- [`documentation/MCP-SERVERS.md`](documentation/MCP-SERVERS.md) — every third-party server, its pin, licence and allowlist
- [`documentation/DESIGN-NOTES.md`](documentation/DESIGN-NOTES.md) — the long-form build narrative: what was found, what was wrong, and why each boundary is shaped this way
