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
     ├── stdio MCP ──▶  docs_agent       draft_proposal
     │                  Proposal drafting + fpdf2 PDF. Identity, rates and
     │                  signature come from config/juma.json IN CODE. No SMTP,
     │                  no ClickUp, no bot token — it cannot reach a client.
     │
     ├── stdio MCP ──▶  n8n_agent        list_workflows · run_workflow
     │                  Named workflows only, by webhook. The n8n ADMIN key is
     │                  not declared in its bootstrap, so it is not in-process.
     │
     ├── stdio MCP ──▶  kola_agent       kola_catalogue · kola_query
     │                  Kolaborate marketplace, read-only. Its `kola_call` names
     │                  the operation in its ARGUMENTS, so the agent allowlists
     │                  the INNER operation — no tool-name allowlist can.
     │
     └── stdio MCP ──▶  coding_agent     start_code_task · get_status
                        · get_result · list_changed_files
                        · github_query · github_action · docs_query
                        Pi in Docker. Confined to the dev root, new branch per
                        task, never pushes, .env files masked out of the mount.
                             │
                             ├── MCP client ──▶ github-mcp-server (Docker)
                             │                  32 tools upstream, 13 allowed.
                             │                  No merge, no delete, no admin,
                             │                  no workflows.
                             │
                             └── MCP client ──▶ context7 (node)
                                                library docs, read only.
```

Third-party MCP servers attach to an **agent**, never to Hermes — the agent is
the MCP client, applies its own allowlist, and exposes only its own tools. See
`documentation/MCP-SERVERS.md`.

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
| Fallback 1 | Gemini `gemini-3.1-flash-lite` | OpenRouter |
| Fallback 2 | OpenRouter `nvidia/nemotron-3-ultra-550b-a55b:free` | — |
| Limit that binds | 250,000 input tokens/**day**, per model | 8,000 tokens/minute |

Hermes cannot run on Groq. Its Telegram tool surface measures ~6,900 tokens per
model call, a routing turn needs at least two calls, and every tool-capable Groq
model is capped at 8,000 tokens per minute. The two Groq models with a higher
cap refuse tool calling outright, which is the only thing an orchestrator does.
The arithmetic and the measurements are in `hermes/configure_providers.py`.

OpenRouter ran Hermes for a while and works, but its free tier allows 50
requests per **day**, account-wide — roughly a dozen routing turns, after which
the orchestrator is down until 00:00 UTC. It is a sound last resort and a poor
primary, so that is where it sits now: fallback 2, behind a second Gemini model.

The model on that tier changed from `nvidia/nemotron-3.5-lightning:free` to
`nvidia/nemotron-3-ultra-550b-a55b:free`, on evidence rather than benchmarks.
Lightning carried two live turns during end-to-end testing and misreported in
both: it told the operator a program had been run that the coding agent's own
log shows was never executed, and it appended two unrelated sentences about
compliance workflows to a report about a text file. Both are in
`documentation/E2E-RESULTS.md`. A fallback that answers confidently and wrongly
is worse than one that is unavailable, because the unavailable one is visible.

Each tier is proven, not assumed — one real routing turn each, forced by
breaking the tier above it:

| tier | forced how | served by | result |
|---|---|---|---|
| primary | nothing broken | `gemini/gemini-3.5-flash-lite` | 2 api calls, real ClickUp data |
| fallback 1 | primary model → a name that 404s | `gemini/gemini-3.1-flash-lite` | 2 api calls, real ClickUp data |
| fallback 2 | both Gemini tiers 404ing | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | 2 api calls, real ClickUp data |

### `reasoning_effort: "none"` and the provider entry that does nothing

`transports/chat_completions.py` encodes "thinking off" as reasoning effort
`"none"`, which is not in the vocabulary Gemini or Groq accept — they return
HTTP 400. The workaround in the config was
`providers.groq.extra_body.reasoning_effort: medium`.

**That entry never did anything.** `agent_init._custom_provider_extra_body_for_agent()`
returns `None` unless the provider name is `custom` or `custom:<key>`, so a
`providers.<builtin>.extra_body` block is silently ignored. Measured on a forced
OpenRouter turn:

```
Fallback nvidia/nemotron-3-ultra-550b-a55b:free: reasoning_config resolved: {'enabled': True, 'effort': 'medium'}
Fallback nvidia/nemotron-3-ultra-550b-a55b:free: extra_body resolved: None
```

The turn still completed with a tool call and no 400 — because what actually
keeps `"none"` off the wire is `agent.reasoning_effort: medium`, resolved on a
different path entirely. So the dead entry is gone, and the live value is
asserted in `configure_gemini.py` instead of assumed. A control that reads as
present and is not is the same failure this repository already had once.

Gemini's free tier meters requests per **minute**, which recovers in seconds —
and that was the whole reason for choosing it. **It was not the whole story,
and the part that was missing is the part that binds.** The live end-to-end run
produced this, mid-turn, on a routing task:

```
429 RESOURCE_EXHAUSTED: Quota exceeded for metric:
generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
limit: 250000, model: gemini-3.5-flash-lite
```

The free tier meters **both**, and the second one is a *daily* input-token cap.
One measured routing turn costs 13,161 input tokens, so 250,000 is about
nineteen turns a day. A per-minute limit recovers while you wait; this one does
not come back until the day rolls over.

That is what actually took the orchestrator off its primary during testing —
two of five live turns finished on the fallback — and it is why there are now
two fallback tiers rather than one. A single OpenRouter tier behind Gemini is
not depth: OpenRouter's free tier is 50 requests per **day**, account-wide, so
both run out on the same afternoon.

**The quota is per model.** The 429 names the model inside the metric, so a
different Gemini model is a different 250k bucket on the same key — at no cost
and with no new credential. That is what fallback 1 is. Measured on this key:

| model | function calling | verdict |
|---|---|---|
| `gemini-3.1-flash-lite` | HTTP 200, `tool_calls=1` | fallback 1; 1M context, own quota bucket |
| `gemini-2.5-flash-lite` | HTTP 404 | "no longer available to new users" |

Not `gemini-flash-lite-latest` and not a `-preview` suffix: an alias can move
under a running gateway, which is the same reason the sandbox pins RTK.

Model choice within the tier is not free either, measured on this key:

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
                    `hermes update` stashes local changes to its checkout.
                    surface-manifest.json is the tool allowlist; SOUL.md is the
                    tracked copy of %LOCALAPPDATA%\hermes\SOUL.md; post_update.py runs the
                    rest and proves it
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
docker build -t juma-pi-sandbox:2 agents/coding      # sandbox for the coding agent
(cd agents/coding/vendor && npm ci)                   # the Pi coding agent, from the lockfile

python hermes/post_update.py                 # everything below, then proves it
```

### After every `hermes update`, run one command

```bash
python hermes/post_update.py
```

Hermes is a git checkout and `update` pulls into it with
`non_interactive_local_changes: stash`, so **every local change to it is
temporary by default**. Two of the five invariants live inside that checkout —
the approval patch and the in-process guard — and a third, the launcher, lives
in a file `hermes gateway install` regenerates from a template. All three are
one update away from silently not existing, and that has already happened once.

`post_update.py` re-applies all of it and then proves it, in this order:

| step | |
|---|---|
| 1 | `configure_orchestrator.py` — MCP servers and include lists |
| 2 | `harden_telegram_surface.py` — Telegram allowlist + in-process guard |
| 3 | `configure_gemini.py restore` — model chain, reasoning-effort assertion |
| 4 | `apply_approval_patch.py` — secret-store reads require approval |
| 5 | `gateway_launcher.py --install` — the task reaches the guard |
| 6 | `check_telegram_surface.py` — all seven conditions, under Hermes' interpreter |
| 7 | `tests/test_surface_guard.py` — the negative tests |

6 before 7 on purpose: 6 says the machine is in the right state, 7 says the
check that decided that is still capable of saying no. **A green 6 with a
broken 7 is the failure that matters, because it looks exactly like success.**

The individual scripts still exist and are still idempotent; `post_update.py`
is the thing to actually run.

Each agent needs its own `agents/<name>/.env` (gitignored). Key names only:

| agent | keys |
|---|---|
| research | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `RESEARCH_MODEL` |
| pm | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CLICKUP_TOKEN`, `CLICKUP_TEAM_ID`, `PM_MODEL` |
| coding | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CODING_MODEL`, `CODING_ROOT`, `CODING_SANDBOX` |

Hermes itself needs `GEMINI_API_KEY` (its primary) and `OPENROUTER_API_KEY`
(its fallback). `GEMINI_API_KEY` is read from the environment, so it does not
have to be written into `%LOCALAPPDATA%\hermes\.env`.

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
every `hermes update`** — or just run `python hermes/post_update.py`, which
re-applies everything and proves it.

### The guard checks six things, not one

A clean tool surface is necessary and nowhere near sufficient, so
`hermes/check_telegram_surface.py` now refuses the start on any of:

| # | condition | why it is fatal |
|---|---|---|
| 1 | a tool outside the manifest is on the wire | the original defect |
| 2 | the approval patch is missing | `hermes update` stashed it once already |
| 3 | `command_allowlist` is non-empty | every entry runs without asking |
| 4 | the Telegram allow-list is not exactly the one user | it is the entire authentication boundary |
| 5 | an MCP `tools.include` disagrees with the manifest | an agent grew a tool nobody declared |
| 6 | a surface tool has no owner in the manifest | something arrived from outside the model |

All six fail closed, and **a check that raises counts as failed** — an
unprovable boundary is treated as a breached one.

Condition 4 compares a **SHA-256**, not the ID. The ID is the whole
authentication boundary and this is a public repository, so the manifest pins
`telegram_allowed_users_sha256` and the value itself is never written down. A
changed ID still fails the check.

### `hermes/surface-manifest.json`

Every tool that may reach the Telegram surface, with its owner — one entry per
tool, the value being the MCP server that provides it or `hermes-builtin`. The
per-server `tools.include` lists are *derived* from it by grouping, so a tool is
declared in exactly one place and there is no second list to drift.

Adding a tool to an agent is therefore two edits on purpose: the agent's
`tools.include`, and the manifest. Conditions 5 and 6 compare them on every
start, in both directions.

### A refusal used to be silent

The guard worked and nobody could tell. It logs its verdict at INFO and
CRITICAL from inside `start_gateway()`, which runs **before Hermes attaches its
file handlers** — so neither line appears in `gateway.log`, at any start, ever.
Checked directly; there are no matches in the file.

That left the worst available failure mode: the gateway refuses, exits
non-zero, Task Scheduler retries and gives up, and the first symptom is that
Telegram has gone quiet — indistinguishable from a flat phone battery.

`hermes/gateway_launcher.py` runs the guard *in front of* the gateway, so the
reporter outlives the verdict, and announces it on three channels:

| channel | note |
|---|---|
| Telegram | one fixed text, naming no tool, key or value — it goes to a phone, and the premise is that the surface may be open |
| Windows Event Log | Application / `Hermes_Gateway`, id 101 on refusal, 100 on pass |
| `logs/gateway-guard.log` | always works; the other two can be unregistered or offline |

Proven by breaking condition 3 for real: the guard refused, `telegram: sent`,
the gateway did not start, exit 1, and the Python process count was unchanged.

The in-process guard **stays**. It is not redundant — it covers
`hermes gateway start/restart` and embedded callers, none of which come through
the launcher. The launcher covers the scheduled task, which is the only route
that runs unattended.

## Where the secrets live

`%LOCALAPPDATA%\hermes\.env` — **not** `~/.hermes`, which is what several
documents said until 2026-09-23. That wrong path had a consequence: a rotation
updated all six agent `.env` files and missed Hermes' own, so the gateway came
up with a revoked bot token and refused to connect.

**The Telegram bot token is no longer in `config.yaml`.** Hermes resolves it
from `TELEGRAM_BOT_TOKEN` in the environment
(`gateway/config_env.py: _Cred(Platform.TELEGRAM, ("TELEGRAM_BOT_TOKEN",))`),
and the gateway already loads that `.env` at startup, so one copy is enough.

That mattered more than tidiness. Every script in `hermes/` copies
`config.yaml` aside before editing and nobody pruned them: **49 backups had
accumulated, all 49 holding the token in plaintext**, and none of them covered
by the approval patch — which matches `config.yaml`, `.env`, `auth.json` and
`mcp-tokens`, not `config.yaml.bak-20260921-110148`. Rotating the token fixed
one copy and left forty-nine. `hermes/backup_prune.py` now keeps the newest
three and runs at the moment a backup is written, and with the token out of
`config.yaml` a backup of it is no longer a copy of a secret.

`allow_list` stays in `config.yaml`: it is an identifier, not a credential, and
guard condition 4 checks it by SHA-256 on every start.

## The approval patch

`hermes/apply_approval_patch.py` makes reading Hermes' secret store require
approval. Upstream covers *writes* to `%LOCALAPPDATA%\hermes\.env`; reads were uncovered,
and the read is the whole prize — every provider key and the Telegram bot token
live in that one file.

It matches on the path rather than on a read verb, because the verb list is
unbounded: `cat`, `type`, `more`, `head`, `tail`, `strings`, `xxd`, `grep`,
`sed -n`, `findstr`, `Get-Content`, `Select-String`, `python -c open()`,
`node -e readFileSync()`, and whatever ships next year.

Re-run it after every `hermes update`. Hermes is a git checkout and `update`
stashes local changes, so the previous patch vanished silently — the verdict for
`cat %LOCALAPPDATA%\hermes\.env` was back to `allow` and nothing said so.

## Tests

```bash
agents/.venv/Scripts/python tests/test_common_scaffold.py
agents/.venv/Scripts/python tests/test_surface_guard.py    # the guard's NEGATIVE tests
agents/.venv/Scripts/python tests/test_research_agent.py   # --live for a real search
agents/.venv/Scripts/python tests/test_pm_agent.py         # --live hits real ClickUp
agents/.venv/Scripts/python tests/test_coding_agent.py     # --live runs Pi
```

298 assertions, 0 failing, as of 2026-09-22.

`test_surface_guard.py` is deliberately almost all negatives. The guard
returning OK on a healthy machine proves almost nothing — a function that
returns `(True, "")` unconditionally passes that test too. So each of the seven
conditions gets a deliberately broken input, and each is asserted to fail *and*
to say why. None of it edits config.yaml or the Hermes checkout: a test that has
to break the running system will eventually be run by someone who forgets to put
it back.

Its one positive test is the exception that has to be live:
`test_annotations_match_the_manifest` starts all three agents over real MCP
stdio and reads each tool's `readOnlyHint` **off the wire**, because that
annotation is what Hermes' write-approval gate keys on. A write tool that gained
`readOnlyHint: true` would silently lose its approval prompt while every static
check in the file still passed.

The agents are exercised over real MCP stdio, not by importing their functions:
that is the only way to catch a server that fails to start, a malformed tool
schema, or a stray `print()` corrupting the JSON-RPC stream.

The security properties are the majority of the suite on purpose. "It works" is
cheap to re-establish; "it cannot escape" has to be re-established on every
change.

## Operations

```bash
python hermes/post_update.py                  # after every `hermes update`
python hermes/check_keys.py                   # is every credential still alive?
python hermes/redact_docs.py --check          # anything exposed in documentation/?
python hermes/gateway_launcher.py --check     # the guard, without starting anything
python hermes/gateway_launcher.py --test-alert # prove the refusal alert still works
```

### `check_keys.py`

One authenticated read-only call per credential, in every `.env`, reporting a
verdict per key **name**. Never a value — not a prefix, not a suffix, not four
characters "for identification". Lengths are printed, because a 12-character key
is a truncated paste and that is a different problem from a rejected one.

Where a name has no validator it says `unknown` rather than guessing. An
unchecked key reported as `unknown` is honest; reported as `ok` it is a lie that
will be believed for months.

The same name appears in several `.env` files by design — per-agent isolation is
an invariant and one shared file would defeat it — so the copies are compared
**by digest** and a drift is reported without any value leaving the process.

One caution it was built from: Groq sits behind Cloudflare, which rejects
urllib's default User-Agent with `HTTP 403 error code: 1010`. That is an edge
block, not an auth failure, and the first version of this script reported a
working key as INVALID in all four `.env` files while the agents were using it
successfully in the same minute. Every request now carries a real User-Agent.

### `redact_docs.py`

`documentation/AUDIT-agentic-os.md` raised this against the README and it was
just as true of the audit itself: publishing both bot IDs plus the one
allow-listed chat ID hands an attacker the exact target set. A bot ID is the
numeric prefix of its token; the chat ID is the whole authentication boundary.

106 values across 14 tracked documents are now redacted, and `--check` fails if
any come back.

It does **not** rewrite git history. Those values are in old commits and they
stay there — rewriting every hash in an already-pushed repository, for values
that must be treated as disclosed either way, buys nothing. Rotate what can be
rotated, stop publishing it going forward, and say so.

It also leaves `tests/` and `archive/pre-hermes/` alone: those contain
deliberately fake credentials whose purpose is to prove the redaction layer
removes them. Scrubbing them would delete the evidence that redaction works.

## Write approval

Three tools change something outside this machine: `pm_action`,
`start_code_task` and `github_action`. Each one now requires a human to approve
it **before the call is sent**, and that is enforced by Hermes, not asked for in
a prompt.

`mcp_servers.<name>.trust: untrusted` arms Hermes' own gate
(`tools/mcp_tool_handlers.py:_trust_gate_check`); the `readOnlyHint` annotation
each agent publishes aims it. On an untrusted server, a tool whose hint is not
exactly `True` is gated. So the three write tools are deliberately *not*
annotated, every read tool is, and no plugin was written — Hermes already had
this and a second approval path beside a working one would be worse than none.

Verified by calling the gate directly under Hermes' interpreter, with no model
involved:

| case | result |
|---|---|
| untrusted + `pm_action` (write) | **BLOCKED** |
| untrusted + `pm_query` (`readOnlyHint: true`) | allowed |
| untrusted + unknown tool (no hint) | **BLOCKED** |
| untrusted + hint is the *string* `"true"` | **BLOCKED** |
| `trust: full` + `pm_action` | allowed — **the gate is disarmed** |

That last row is why guard condition 7 exists. `full` is Hermes' compatibility
default, so an `mcp_servers` block written by hand, or regenerated by an older
script, arrives with approval off and nothing says so.

Full detail, including the GitHub and Context7 allowlists, is in
`documentation/MCP-SERVERS.md`.

## Known open items

Carried forward deliberately, with what each one needs. Items that were open in
earlier revisions and have since been closed are listed at the bottom, because
a list that only ever grows stops being read.

| item | needs |
|---|---|
| **`TELEGRAM_BOT_TOKEN` is rejected — the gateway cannot connect** | a fresh token from BotFather, pasted into `%LOCALAPPDATA%\hermes\.env`. Everything else in the chain is verified; this one value is revoked. Until it is replaced, no live Telegram test can run |
| Three live Telegram re-tests outstanding | blocked on the line above. They are: the `docs_query` retry loop, `github_query` routing, and the coding agent's no-longer-false completion claim. All three fixes are verified locally and by test; none has been exercised over a real message |
| Telegram approval prompt unexercised from a phone | the write-approval gate is proven deterministic under Hermes' own interpreter, and the elicitation patch is proven to offer only `['once','deny']`. The round trip to a handset has not been done |
| n8n is wired but runs no live workflow | by choice. Driving a real workflow would mean putting a second copy of the ClickUp credential inside n8n, and one copy per credential is the rule that makes per-agent isolation mean anything. The HMAC boundary, the receiver and `run_workflow` are all tested; the workflow behind them is a stub |
| Disclosed identifiers are not rotated | the bot token and chat ID were published in this repository's history. Redaction stops new exposure; it does not undo the old one. The token is being rotated anyway for the reason in row 1 |
| `documentation/STATUS.md` records `external_action.py` still POSTing to a dead `/message/send` route | archived pre-Hermes code, unreachable from any production path; left as history rather than repaired |

### Closed since the last revision

| was | now |
|---|---|
| Scheduled task restarted 999 times, not 3 | `RestartCount: 3`, `RestartInterval: PT1M` |
| Windows Event Log alerts were not written | source `Hermes_Gateway` exists; 100 pass, 101 refusal |
| Kolaborate MCP not wired | wired read-only behind `kola_agent`, returning real marketplace data, with the **inner** operation allowlisted |
| CI had never passed | `quality` is green. Four separate causes, recorded in `documentation/STATUS.md` |
