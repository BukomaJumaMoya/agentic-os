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
     └── stdio MCP ──▶  coding_agent     start_code_task · get_status
                        · get_result · list_changed_files
                        Pi in Docker. Confined to the dev root, new branch per
                        task, never pushes, .env files masked out of the mount.
```

The server is `coding_agent`, not `coding`, and the name is load-bearing —
see "The Telegram tool surface" below.

Each agent is a standalone process, its own model, its own `.env`. Hermes holds
none of their keys, and each agent scrubs every inherited credential out of its
environment before loading its own file — so the research agent cannot reach a
ClickUp token even by accident.

## Model providers

Split deliberately, because the free tiers fail in different ways:

| | Hermes | Agents |
|---|---|---|
| Provider | Gemini (Google AI Studio) | Groq |
| Model | `gemini-3.5-flash-lite` | `openai/gpt-oss-120b` |
| Fallback | OpenRouter `nvidia/nemotron-3.5-lightning:free` | OpenRouter |
| Limit that binds | requests/minute | 8,000 tokens/minute |

Hermes cannot run on Groq. Its Telegram tool surface measures ~6,900 tokens per
model call, a routing turn needs at least two calls, and every tool-capable Groq
model is capped at 8,000 tokens per minute. The two Groq models with a higher
cap refuse tool calling outright, which is the only thing an orchestrator does.
The arithmetic and the measurements are in `hermes/configure_providers.py`.

OpenRouter ran Hermes for a while and works, but its free tier allows 50
requests per **day**, account-wide — roughly a dozen routing turns, after which
the orchestrator is down until 00:00 UTC. It is a sound fallback and a poor
primary, so that is where it sits now.

Gemini's free tier meters requests per **minute**, which recovers in seconds.
Model choice within it is not free either, measured on this key:

| model | free-tier limit | verdict |
|---|---|---|
| `gemini-3.7-flash` | **5 requests/minute** | unusable — one routing turn costs 4-5 calls, so the turn 429s on itself |
| `gemini-3.5-flash-lite` | 10+/minute, cap not reached | in use; 3 routing turns, 0 × 429 |

Both emit `tool_calls` correctly; the limit is the whole difference. Gemini
returns **no rate-limit headers at all** — not on 200s, not on 429s, on either
the native or the OpenAI-compatible surface. The only quota signal is the text
of a 429 body, which is also where the per-minute number above came from.

Measured through `tools/logging_proxy.py`, three routing turns:

| | calls/turn | prompt tokens/call | latency/call |
|---|---|---|---|
| research turn | 3 | 6,137 → 7,307 | 2.0–10.0s |
| pm turn | 2 | 6,126 → 6,612 | 2.0–2.4s |
| mixed turn | 3 | 6,143 → 7,625 | 12.8–17.9s |

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

**Both stay on by default anyway**, and the measurement above is the reason to
be explicit about why rather than quietly leaving them on. A fizzbuzz is the
worst case for both tools and close to the best case for the baseline: it
amortises none of Ponytail's fixed ~2.4k-token cost and gives RTK no bulky
command output to compress. Real work inverts both — a repository the model has
to explore produces exactly the long `ls`, `grep` and test output RTK exists to
shrink, and enough turns for a fixed prompt cost to disappear into. The 12-vs-18
lines and 2-vs-5 round trips point that way, and those are the numbers that
scale with task size; the token deficit is the one that does not.

So the default is a bet on the shape of real tasks, not a conclusion drawn from
this one. **It should be re-measured on a genuine project task** — a change
against a real repository, with real exploration — before the default is
treated as settled either way. Until then, the honest summary is that on small
tasks these tools cost more than they save, and that small tasks are not what
they are for.

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
tools/              logging_proxy.py — sits between Hermes and a provider and
                    records what is actually sent; --sink answers locally so a
                    tool surface can be inspected without spending quota
tests/              real MCP stdio clients, not mocks
evidence/           measurement output (gitignored)
archive/pre-hermes/ the previous enquiry/proposal pipeline and its tests
```

## Setup

```bash
python -m venv agents/.venv
agents/.venv/Scripts/pip install -r requirements.txt
docker build -t juma-pi-sandbox:1 agents/coding      # sandbox for the coding agent
(cd agents/coding/vendor && npm install)              # the Pi coding agent

python hermes/apply_approval_patch.py       # re-run after every `hermes update`
python hermes/harden_telegram_surface.py    # re-run after every `hermes update`
python hermes/configure_providers.py
python hermes/configure_orchestrator.py
```

Both patched scripts edit the Hermes checkout, and `hermes update` stashes
local changes silently. Re-running them is the only thing that makes either
patch durable.

Each agent needs its own `agents/<name>/.env` (gitignored). Key names only:

| agent | keys |
|---|---|
| research | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `RESEARCH_MODEL` |
| pm | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CLICKUP_TOKEN`, `CLICKUP_TEAM_ID`, `PM_MODEL` |
| coding | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CODING_MODEL`, `CODING_ROOT`, `CODING_SANDBOX` |

Hermes itself needs `GEMINI_API_KEY` (its primary) and `OPENROUTER_API_KEY`
(its fallback). `GEMINI_API_KEY` is read from the environment, so it does not
have to be written into `~/.hermes/.env`.

## The Telegram tool surface

The claim at the top of this file — that Hermes has no terminal, no file
access, no code execution and no browser — was **false in the running system**,
and every diagnostic that reads `config.yaml` said otherwise. `hermes tools
list` and `hermes tools --summary` both reported the six restricted toolsets.
The request on the wire carried **24 tools**, including `terminal`,
`write_file`, `patch`, `execute_code`, `delegate_task` and the whole browser
toolset. Anyone with the Telegram bot could run a shell command.

It was not a setting anyone changed. It was a name collision:

1. `hermes_cli/tools_config.py:686` — when a platform's toolset list names no
   real MCP server, Hermes merges *every* enabled MCP server name into the
   enabled-**toolset** list. Ours were `research`, `pm` and `coding`. (The list
   did say `mcp-research`/`mcp-pm`/`mcp-coding`, but those are neither server
   names nor toolset names, so they resolved to nothing and the default merge
   fired.)
2. `toolsets.py:177` — Hermes ships a built-in toolset called `coding`: "files,
   terminal, search, web docs, skills, todo, delegate, vision, browser".
3. `model_tools.py:324` — the merged name is resolved as a toolset.
   `resolve_toolset("coding")` returns 37 tools. `research` and `pm` resolve to
   0 and were harmless; `coding` alone opened the surface.

Upstream made this reachable while fixing the opposite bug. Issue #30563 (an
MCP server named after a built-in was *shadowed* by it) was closed by #103943,
which changed `toolsets.get_toolset()` to **union** the two — see the comment
at `toolsets.py:289`. That fixed the missing-tools complaint and turned a
shadowing bug into a privilege-expansion one.

### The fix

`python hermes/harden_telegram_surface.py` — config first, idempotent:

- renames the MCP server `coding` → `coding_agent`, removing the collision at
  source (the directory stays `agents/coding`);
- names the three real servers in `platform_toolsets.telegram`, which turns
  Hermes' "merge every enabled server" default into an explicit allowlist, so a
  fourth agent added later is not automatically on the Telegram surface;
- sets `tools.tool_search.enabled: off`. With seven agent tools there is
  nothing to page, and deferring them behind `tool_search`/`tool_describe`/
  `tool_call` costs an extra model call per turn to look up tools that fit
  comfortably in the prompt.

| | before | after |
|---|---|---|
| tools on the wire | 24 | 10 |
| tool schemas | 41,566 chars | 12,378 chars |
| system prompt | 20,823 chars | 13,470 chars |
| whole request | 62,246 chars | 27,356 chars |
| forbidden tools | 11 | 0 |

The surface is now exactly `clarify`, `memory`, `session_search` and the seven
agent tools, and the agent tools are eager rather than behind a bridge.

**Before naming a fourth agent, check the name against `hermes tools list`.**
A server named after any built-in toolset grants that whole toolset.

### The guard

The fix is a config change, and config is precisely what proved unreliable here
— every reading of it was correct while the surface was open. So
`harden_telegram_surface.py` also patches `start_gateway()` in Hermes'
`gateway/run.py` to re-derive the surface from Hermes' own resolver on every
start and refuse to start when a forbidden tool appears:

```
CRITICAL gateway.run: REFUSING TO START -- Telegram tool surface:
34 forbidden tool(s): browser_back, ... terminal, ... write_file.
```

Verified by deliberately re-introducing the collision: the gateway refused,
exited non-zero, and never connected to Telegram.

It is patched into `start_gateway()` and not `main()` on purpose. `main()` is
only the argv entry point of `python -m gateway.run`; `hermes gateway
start/restart` and the scheduled task both run `python -m hermes_cli.main
gateway run`, which reaches `start_gateway()` without passing through `main()`.
A guard in `main()` was tried first, and a deliberately broken config started
and connected to Telegram anyway.

It fails closed in both directions: if the check cannot be imported or raises,
the gateway does not start. An unprovable boundary is treated as a breached one.

Like the approval patch, this edits the Hermes checkout, so **re-run it after
every `hermes update`**. `hermes/check_telegram_surface.py` runs the same check
on its own, under Hermes' interpreter.

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
