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

## Token reduction in the coding sandbox

Two third-party tools run inside `juma-pi-sandbox`, and nowhere else. Both are
pinned, because they are code executing inside a confinement boundary with a
model driving them — `latest` would let the sandbox change under a rebuild.

| tool | pinned to | what it does |
|---|---|---|
| [RTK](https://github.com/rtk-ai/rtk) | `v0.49.0` | rewrites shell commands so their output costs fewer tokens |
| [Ponytail](https://github.com/DietrichGebert/ponytail) | commit `e3ba2aa` (v4.10.0) | a ruleset pushing the model to write less code |
| Pi | `0.86.1` | the coding agent itself |

`v0.49.0` is RTK's newest **non**-prerelease; every tag after it is a
`dev-0.50.0-rc`. The release tarball is checksum-verified at build time against
the `checksums.txt` published with the same tag.

Telemetry is off twice over: RTK's is opt-in and never consented to, and
`RTK_TELEMETRY_DISABLED=1` is set as an image ENV and passed again at run time.
Verified in the container: `env override: RTK_TELEMETRY_DISABLED=1 (blocked)`.

All state stays inside the container — RTK's Pi extension at
`/home/agent/.pi/agent/extensions/rtk.ts`, its runtime data in `/tmp/rtk`,
Ponytail under `/home/agent/.pi/agent/git/…`. A run with the project mounted
confirmed nothing is written to the mounted path.

Neither tool goes anywhere near Hermes. Hermes has no shell, so RTK would have
nothing to compress, and Ponytail injects its ruleset every turn — which would
*add* tokens to the one component already constrained by them.

### Measured, on one small task

`agents/.venv/Scripts/python tests/measure_token_tools.py`

| arm | input | output | model calls | lines written | passed |
|---|---|---|---|---|---|
| RTK + Ponytail | 3,799 | 287 | 2 | 12 | yes |
| baseline | 1,381 | 262 | 5 | 18 | yes |

On a task this small the tools **cost** tokens rather than saving them:
Ponytail's skill catalogue is a fixed ~2.4k-token addition to the system
prompt, which a trivial task cannot amortise, and RTK only pays off when a
command produces bulky output — this one produced almost none. What they did
deliver is a third less code (12 lines vs 18) in fewer round trips.

One run per arm; indicative, not a benchmark. Order matters more than it should:
Groq's free tier is 8,000 tokens per minute, and whichever arm runs second
inside one window returns an empty completion. That artifact initially looked
exactly like the tools breaking the agent, which is why the harness now reports
a zero-token arm as INCONCLUSIVE rather than as a failure.

Set `CODING_TOKEN_TOOLS=off` in `agents/coding/.env`, or pass
`token_tools: false` to `start_code_task`, to run without them.

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
