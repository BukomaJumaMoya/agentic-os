# agentic-os

An assistant for a one-person software consultancy. Requests arrive over
Telegram, Hermes decides which specialist should handle them, and the specialist
does the work in its own process with its own credentials.

Hermes orchestrates and nothing else. It has no terminal, no file access, no
code execution and no browser. Everything that can touch the world lives behind
an agent boundary with its own confinement, and each boundary is enforced in
code rather than asserted in a prompt.

## Architecture

```
  Telegram
     │
     ▼
  Hermes  (sole orchestrator — routes, polls, reports; no tools of its own)
     │
     ├── stdio MCP ──▶  research agent   research(question, depth)
     │                  Tavily + model. Read only. No files, no shell,
     │                  no route to any host but its search API and model.
     │
     ├── stdio MCP ──▶  pm agent         pm_query(question) · pm_action(instruction)
     │                  ClickUp. Create and update only — the DELETE verb has
     │                  no code path at all.
     │
     └── stdio MCP ──▶  coding agent     start_code_task · get_status
                        · get_result · list_changed_files
                        Pi in Docker. Confined to the dev root, new branch per
                        task, never pushes, .env files masked out of the mount.
```

Each agent is a standalone process, its own model, its own `.env`. Hermes holds
none of their keys, and each agent scrubs every inherited credential out of its
environment before loading its own file — so the research agent cannot reach a
ClickUp token even by accident.

## Model providers

Split deliberately, because the two free tiers fail in opposite ways:

| | Hermes | Agents |
|---|---|---|
| Provider | OpenRouter | Groq |
| Model | `nvidia/nemotron-3-ultra-550b-a55b:free` | `openai/gpt-oss-120b` |
| Limit that binds | 50 requests/day, account-wide | 8,000 tokens/minute |

Hermes cannot run on Groq. Its Telegram tool surface measures ~6,866 tokens per
model call, a routing turn needs at least two calls, and every tool-capable Groq
model is capped at 8,000 tokens per minute. The two Groq models with a higher
cap refuse tool calling outright, which is the only thing an orchestrator does.
The arithmetic and the measurements are in `hermes/configure_providers.py`.

## Layout

```
agents/_common/     shared scaffold: env scrubbing, structured errors and
                    redaction, per-agent audit log, provider chain, job
                    registry, untrusted-content fencing, path confinement
agents/research/    Tavily research, read only
agents/pm/          ClickUp, no delete
agents/coding/      Pi coding agent, Docker-sandboxed
hermes/             the Hermes-side configuration, kept in the repo because
                    `hermes update` stashes local changes to its checkout
tests/              real MCP stdio clients, not mocks
archive/pre-hermes/ the previous enquiry/proposal pipeline and its tests
```

## Setup

```bash
python -m venv agents/.venv
agents/.venv/Scripts/pip install -r requirements.txt
docker build -t juma-pi-sandbox:1 agents/coding      # sandbox for the coding agent
(cd agents/coding/vendor && npm install)              # the Pi coding agent

python hermes/apply_approval_patch.py     # re-run after every `hermes update`
python hermes/configure_providers.py
python hermes/configure_orchestrator.py
```

Each agent needs its own `agents/<name>/.env` (gitignored). Key names only:

| agent | keys |
|---|---|
| research | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `RESEARCH_MODEL` |
| pm | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CLICKUP_TOKEN`, `CLICKUP_TEAM_ID`, `PM_MODEL` |
| coding | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CODING_MODEL`, `CODING_ROOT`, `CODING_SANDBOX` |

## The approval patch

`hermes/apply_approval_patch.py` makes reading Hermes' secret store require
approval. Upstream covers *writes* to `~/.hermes/.env`; reads were uncovered,
and the read is the whole prize — every provider key and the Telegram bot token
live in that one file.

It matches on the path rather than on a read verb, because the verb list is
unbounded: `cat`, `type`, `more`, `head`, `tail`, `strings`, `xxd`, `grep`,
`sed -n`, `findstr`, `Get-Content`, `Select-String`, `python -c open()`,
`node -e readFileSync()`, and whatever ships next year.

Re-run it after every `hermes update`. Hermes is a git checkout and `update`
stashes local changes, so the previous patch vanished silently — the verdict for
`cat ~/.hermes/.env` was back to `allow` and nothing said so.

## Tests

```bash
agents/.venv/Scripts/python tests/test_common_scaffold.py
agents/.venv/Scripts/python tests/test_research_agent.py   # --live for a real search
agents/.venv/Scripts/python tests/test_pm_agent.py         # --live hits real ClickUp
agents/.venv/Scripts/python tests/test_coding_agent.py     # --live runs Pi
```

The agents are exercised over real MCP stdio, not by importing their functions:
that is the only way to catch a server that fails to start, a malformed tool
schema, or a stray `print()` corrupting the JSON-RPC stream.

The security properties are the majority of the suite on purpose. "It works" is
cheap to re-establish; "it cannot escape" has to be re-established on every
change.
