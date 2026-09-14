# agentic-os

An assistant for a one-person software consultancy. A client enquiry arrives over
Telegram, three specialist agents gather what they can, a model drafts a
proposal, and nothing reaches anyone until a human approves it.

It is a bounded pipeline, not an autonomous agent. Every step is a subprocess
with a fixed contract, and the only thing that can send a message is code behind
an approval gate.

## What actually happens

```
 Telegram  ──▶  OpenClaw gateway  ──▶  /apr_enquiry  ──▶  job_runner (detached, tracked)
                                                               │
                              ┌────────────────────────────────┤
                              ▼                ▼               ▼
                        research agent   projects agent   coding agent
                         (Tavily)        (ClickUp, RO)   (feasibility)
                              └────────────────┬───────────────┘
                                               ▼
                                     proposal synthesis  ──▶  provider chain
                                               │                (Groq → Gemini
                                               ▼                 → OpenRouter)
                                     approval request on disk
                                               │
                              ┌────────────────┴────────────────┐
                              ▼                                 ▼
                    approval prompt (gateway)          PDF (Telegram Bot API)
                              │
                       /apr_approve <id>
                              ▼
                     external action executes
```

A proposal takes roughly 30 seconds. The slash command acknowledges immediately
and the work happens in a detached child, so the handler never blocks.

## Telegram commands

All are Telegram-only, require authorisation, and are checked twice: by the
gateway against its channel allowlist, and again in `telegram_commands.py`
against the configured user id.

| Command | Does |
|---|---|
| `/apr_enquiry <text>` | Draft a proposal for a client enquiry. Acknowledges at once; the approval prompt follows. |
| `/apr_list` | Pending requests — id, subject, timestamp, status. Summary only. |
| `/apr_status <id>` | One request's status, plus the decision once made. |
| `/apr_approve <id>` | Approve. This is what permits an external action. |
| `/apr_reject <id>` | Reject. |
| `/apr_resume <id>` | Run the external action for an already-approved request. |
| `/apr_jobs` | Recent workflow jobs: id, status, age, and the current checkpoint. |
| `/apr_cancel <id>` | Stop a running job. A short id prefix is enough. |

`/apr_list` and `/apr_status` return identifiers only. They deliberately do not
return the proposal body, the evidence block, or research content — a listing
should not dump client-facing prose and scraped third-party text into a chat.

The `apr_` prefix is not decoration: `approve` is an OpenClaw built-in that would
silently shadow a plugin command, and `status` is reserved outright.

## The three agents

Each is a standalone subprocess speaking JSON over stdin/stdout, with its own
authority level. They are engaged together for every enquiry, and each reports
honestly when it finds nothing.

**research** (`agents/research/main.py`) — Tavily search. A model first extracts
2–4 focused queries from the enquiry, so the *problem domain* is searched rather
than the client's prose. Results are filtered by relevance and dropped if they
read as video transcripts or navigation chrome. If no model is available,
research is skipped rather than run with a bad query. READ only.

**projects** (`agents/projects/main.js`) — ClickUp, read-only at proposal time.
Searches prior tasks for related work and returns **a count, never names**. Task
names identify other clients, and a name reaching the proposal prompt would put
one client's identity into another client's document.

**coding** (`agents/coding/main.py`) — a `feasibility` action that reads the
enquiry as prose and returns a component breakdown plus risks, each bound to a
component. Feeds the proposal's approach section. **Nothing in this agent
executes code**: no `exec`, no `eval`, no import of generated code, no test
runner. Generated Python is checked with `ast.parse`, which builds a syntax tree
and runs nothing.

Their output reaches the model inside one delimited `GATHERED CONTEXT` block,
marked as data rather than instruction. A section appears only when its agent
returned something — so if an agent fails, the proposal says nothing about that
dimension instead of inventing it.

## The provider chain

`orchestrator/llm.py` is the only place that talks to a model.

| Order | Provider | Key | Default model |
|---|---|---|---|
| 1 | Groq | `GROQ_API_KEY` | `openai/gpt-oss-120b` |
| 2 | Gemini (AI Studio) | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| 3 | OpenRouter | `OPENROUTER_API_KEY` | `nvidia/nemotron-3.5-lightning:free` |

Every free tier here rate-limits, so a single provider fails for reasons that
have nothing to do with the enquiry. Providers are tried in order. Rate limits,
5xx, timeouts and rejected credentials all move to the next one; a provider with
no key is skipped silently, so the chain is whatever keys you actually hold.

When every provider declines, it fails loudly. There is no fallback text
anywhere: you get a proposal or an error, never a template.

A reply that arrives but is unusable is **not** a fallthrough. A model answered
and the answer was bad — that is `llm_invalid_output`, and it is terminal.
Asking three providers in turn for prose that passes validation is how a
plausible-but-wrong proposal eventually gets through.

Models are overridable with `GROQ_MODEL`, `GEMINI_MODEL`, `OPENROUTER_MODEL`.

## What the model is not allowed to do

The proposal prose comes from a model. The identity and every number do not.

- `name`, `business` and contact details are appended by code after the model
  returns, so a proposal cannot be signed with anyone else's name
- rate, engagement types and payment terms are read from `config/juma.json` and
  rendered in code; the model is forbidden to state any figure, and never sees
  one
- services are restricted to a candidate list built from config
- validation rejects: quoted currency amounts, placeholder markers, services not
  offered, a missing or over-long subject, and prior-work claims naming anyone

Commercial terms are framed as indicative, not a quote, because the
clarification questions are unanswered — you cannot price work you cannot yet
scope.

## Approval

Nothing external happens without `/apr_approve`. The request, the decision and
the evidence are separate files under `.approval/` and `evidence/`, both
gitignored. A decision is never overwritten by a second one. Re-running a
workflow creates a new request rather than mutating an existing one.

## Running it

Requires Python 3.11+, Node 20+, and an OpenClaw gateway for Telegram.

```bash
git clone https://github.com/BukomaJumaMoya/agentic-os
cd agentic-os
pip install -r requirements.txt      # only needed for PDF rendering
```

Run the workflow directly, without Telegram:

```bash
echo '{"enquiry": "We need appointment reminders from a spreadsheet."}' \
  | python orchestrator/flagship.py
```

Run one agent:

```bash
echo '{"queries": ["appointment reminder automation"], "max_sources": 3}' \
  | python agents/research/main.py
echo '{"action": "list_spaces"}' | node agents/projects/main.js
echo '{"action": "feasibility", "prompt": "..."}' | python agents/coding/main.py
```

Tests — all offline, no keys, no network:

```bash
PYTHONPATH=. python tests/test_step7_flagship_unit.py
PYTHONPATH=. python tests/test_coding_agent.py
node tests/test_projects_agent.js
```

### Environment

| Variable | Needed for |
|---|---|
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | Any model call. At least one. |
| `TAVILY_API_KEY` | Research. Without it, research is skipped. |
| `CLICKUP_TOKEN` | Projects agent. |
| `OPENCLAW_GATEWAY_TOKEN` | Talking to the gateway. |
| `OPENCLAW_ALLOWED_USER_ID` | Who may approve. Falls back to `config/juma.json`. |
| `TELEGRAM_BOT_TOKEN` | PDF delivery. Falls back to OpenClaw's config. |

Optional: `CLICKUP_TEAM_ID`, `TAVILY_SEARCH_DEPTH`, `AGENTIC_STATE_DIR`,
`CODING_AGENT_WORKSPACE`, `AGENTIC_PYTHON`, `OPENCLAW_GATEWAY_URL`.

**No key belongs in `config/juma.json`.** That file is committed. Credentials are
read from the environment, and the code refuses rather than falling back to a
committed file.

## What this deliberately does not do

**External actions deliver to the operator, not to clients.** An approved
proposal is sent to *your* Telegram, not to the client. There is no email
integration and no client-facing send path. You forward it yourself. This is the
single most important thing to understand before trusting the word "approved".

**Nothing executes generated code.** The coding agent writes code and test
source; running it is a separate decision made elsewhere.

**Research is third-party text.** Findings are claims from web pages, not
verified facts, and they are labelled that way where they enter the prompt. They
are also an injection surface: the prompt fences them and instructs the model to
treat them as data, which is mitigation rather than a guarantee.

**Free tiers see your enquiries.** Every provider in the chain is a free tier,
and free tiers are broadly where providers reserve the right to log prompts and
train on them. Treat every run as disclosing the enquiry to whichever provider
serves it. Client work that cannot be disclosed needs a paid tier with a data
processing agreement.

## Status

CI (`quality` and `smoke`) passes. It runs a credential scan over tracked files,
syntax checks, and every test suite — approval boundary, flagship unit, e2e,
failure injection, integration, workspace confinement, research, coding, PDF and
projects. All of them run offline.

Known open items:

- The coding agent's `feasibility` action succeeds about 4 times in 5. The
  failure is its own validation rejecting a risk that names no component — the
  rule is deliberately strict, because a malformed reply is the model missing
  its contract and hiding that would hide the rate.
- `orchestrator/external_action.py` sends to the operator only, as above.
- The Python side now has dependencies (`fpdf2` and its three transitive ones,
  including a compiled Pillow) solely for PDF rendering. Everything else is
  stdlib, and `proposal_pdf` imports lazily, so a machine without them produces
  a proposal with no PDF rather than no proposal.
- A ClickUp personal token was committed in this repository's history inside a
  nested `.git.bak/` object store and is public. It has not been rewritten out.
  Rotate that token.

## Layout

```
agents/         research (Tavily), projects (ClickUp), coding (feasibility)
orchestrator/   llm (provider chain), proposal, research_query, flagship,
                approval, telegram_approval, telegram_commands, jobs, job_runner,
                external_action, proposal_pdf
tools/          OpenClaw plugin registering the /apr_* slash commands
config/         juma.json — identity, services, rates. No credentials.
tests/          offline suites; no network, no API keys
```
