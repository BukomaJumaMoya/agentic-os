# Runbook

Operating the system day to day. Everything here has been run; nothing is
aspirational.

---

## 1. Daily use

Everything happens in one Telegram chat with the Hermes bot. There is no
dashboard and no second interface to learn.

**Reads answer immediately. Writes ask first** — a Telegram card appears with
**Allow Once** and **Deny**. That prompt is not the model being cautious; it is
Hermes' own gate, and the call does not leave the process until you answer.

| you want | say |
|---|---|
| workspace questions | *"List my ClickUp spaces"*, *"What's overdue?"* |
| web research | *"Research X"* — returns a cited summary |
| library docs | *"What does the httpx AsyncClient timeout do? Check the docs."* |
| repository questions | *"In BukomaJumaMoya/agentic-os, what are the last 3 commits?"* |
| a proposal | `proposal: <the client's enquiry, pasted>` |
| a coding task | `code: <ClickUp task id or a description>` |
| create a task | *"Create a task called X in Freelance"* → **approval** |
| open a PR | *"Open a pull request for that branch"* → **approval** |
| client pipeline | *"check client replies"*: reads Airtable only, says it cannot see Gmail; any update → **approval** |

Scheduled, unattended:

| job | when | behaviour |
|---|---|---|
| W2 daily briefing | 07:30 Mon–Fri | overdue + due today; **silent if nothing** |
| W3 weekly review | 08:00 Monday | completed, slipped, stale PRs; silent if nothing |
| n8n notice drain | every minute | delivers queued n8n notices; silent when the queue is empty |

The three client-engine jobs (client-coordinator, reply-processor,
followup-monitor) are **paused**. See section 7.

### One job per session — send `/new` between tasks

This is the one operating habit that matters, and it is a rule rather than a
preference because the failure it avoids is silent.

**Send `/new` when you move to an unrelated task.**

Past roughly 70,000 tokens of history, the router stops calling tools. It does
not error and it does not say so: it answers a documentation question from the
model's own memory instead of reading the docs, and it declines a repository
question with *"I cannot access the private or unindexed repository"* while
holding a working `github_query` and a valid token. Both replies look fine.

Measured on 2026-09-23, the same two questions in two sessions minutes apart:

| | fresh session | loaded session (~72k tokens) |
|---|---|---|
| `docs_query` | **called** | not called |
| `github_query` | **called** | not called |
| input tokens | 8,421 | 72,555 |

**And the session cannot recover by itself.** Hermes' context pruning only runs
*after* a tool call (`turn_preflight.py:369`), so a session whose symptom is
"stopped calling tools" can never be pruned — see
`documentation/upstream-issue-prune-deadlock.md`. Every further message makes it
worse. `/new` is the only free exit.

Nothing is lost: past sessions stay searchable, and `session_search` reaches
them.

### The two prefixes

`proposal:` → research + `pm_query` for context → `draft_proposal` → you get the
draft, the invariant result and **the PDF as a Telegram attachment**. Then it
asks whether to create a ClickUp lead task; that is a separate approval.

`code:` → `pm_query` reads the task if you gave an id → `start_code_task`
(**approval**) → polls → reports the branch, the observed changed files and
whether anything actually ran. A PR is a separate ask and a separate approval.

**Deliverables go to you. Nothing in this system can send anything to a
client** — the docs agent has no SMTP, no ClickUp token and no bot token, so
"email this to them" is not a thing it can be talked into.

---

## 2. What is running

| component | where | notes |
|---|---|---|
| Hermes gateway | scheduled task `Hermes_Gateway`, at logon | restarts 3× at 1-minute intervals |
| 6 agent MCP servers | started by Hermes on demand | stdio children of the gateway |
| n8n | Docker `n8n`, `127.0.0.1:5678` | not reachable off the machine |
| n8n receiver | Docker `n8n-receiver`, network `hermes-n8n` | **no published port at all** |

```
hermes gateway status
docker ps --filter name=n8n
```

---

## 3. After a Hermes update — one command

```bash
python hermes/post_update.py
```

**Run it every time.** Hermes is a git checkout and `hermes update` stashes
local changes, so three patches and the launcher are one update away from
silently not existing. The script re-applies everything and then proves it:

1. MCP servers and include lists
2. Telegram surface + the in-process guard
3. model chain: deepseek-flash alone, checked against `GET /models`
4. approval patch — secret-store reads require approval
5. `readOnlyHint` alias patch — without it every read tool is gated
6. elicitation per-call patch — without it Telegram offers "Always Allow"
7. gateway launcher + scheduled-task policy
8. the startup guard, all nine conditions (9: no fallback chain)
9. the guard's negative tests

Anything but `PASS` at the end means do not start the gateway.

---

## 4. Reading a guard refusal

The gateway refuses to start when any of eight conditions fails. You will know
because **Telegram goes quiet** — and because the launcher sends you a
fixed-text alert saying so.

```
1  tool surface        nothing outside the manifest on the Telegram surface
2  approval patch      reading %LOCALAPPDATA%\hermes\.env still asks
3  command_allowlist   still empty
4  telegram allow-list exactly one user, matched by SHA-256
5  mcp include lists   config.yaml agrees with hermes/surface-manifest.json
6  surface in manifest every resolved tool has a declared owner
7  write approval      every MCP server is trust: untrusted
8  gate sees hints     Hermes' computed readOnlyHint matches the manifest
```

Where to look, in order:

```bash
type %LOCALAPPDATA%\hermes\logs\gateway-guard.log   # the verdict, every start
python hermes/post_update.py                         # usually fixes it
```

Also in the Windows **Event Log** → Application → source `Hermes_Gateway`
(id 100 pass, 101 refusal).

**The most common cause is a stale MCP schema cache**, which is derived data
that can disagree with reality after a patch. The launcher deletes it on every
start; to do it by hand:

```bash
del %LOCALAPPDATA%\hermes\cache\mcp_schema_cache.json
hermes gateway restart
```

**If Telegram is quiet and the guard log has no new line**, the task died
before the guard ran. Run the VBS by hand to see why:

```
cscript //Nologo %LOCALAPPDATA%\hermes\gateway-service\Hermes_Gateway.vbs
```

From 2026-09-22 to 2026-09-30 that printed `Expected end of statement` (a
quoting bug in `gateway_launcher.py --install`, now fixed). The task exited 1
on every start, and no alert fired because the guard never ran.

A refusal is the system working. Do not start the gateway with `--force` or by
editing the guard; fix the condition.

---

## 5. Adding an agent or an MCP server

### A new agent

1. `agents/<name>/main.py` using `_common.bootstrap()`; `agents/<name>/.env`
   with **only that agent's** credentials.
2. Annotate every read tool `annotations={"readOnlyHint": True}`. Leave write
   tools unannotated — that is what makes them ask.
3. **Check the server name against `hermes tools list` first.** A server named
   after a built-in toolset silently grants that whole toolset; that is how 24
   tools including `terminal` once reached Telegram. Use the `_agent` suffix.
4. Add it to `AGENT_SPEC` in `hermes/configure_orchestrator.py`.
5. Add its tools to `hermes/surface-manifest.json`, and any write tool to
   `write_tools`.
6. `python hermes/post_update.py` then `hermes gateway restart`.

The guard will refuse to start if you miss step 4, 5 or the annotations. That
is deliberate.

### A third-party MCP server

**It attaches to an agent, not to Hermes.** The agent is the MCP client
(`_common/mcp_client.py`), applies its own allowlist, and exposes only its own
tools. Two reasons: Hermes' prompt is metered against a daily token cap, and a
tool whose behaviour is defined elsewhere needs policy between it and the
router.

- Pin an exact version or image digest. Record the licence in
  `documentation/MCP-SERVERS.md`.
- Put its credential in the **owning agent's** `.env` only.
- Allowlist the minimum; `Downstream.available()` will flag a stale entry.
- If the server takes a *generic dispatcher* (one tool, operation named in the
  arguments — Kolaborate does this), allowlist the **inner operation**. No
  tool-name allowlist can see it.

---

## 6. Rotating a key

Every credential lives in exactly one `.env`. That is the rule; one copy each.

| credential | file |
|---|---|
| `DEEPSEEK_API_KEY`, `GEMINI_API_KEY` | `%LOCALAPPDATA%\hermes\.env` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` | `%LOCALAPPDATA%\hermes\.env` |
| `TAVILY_API_KEY` | `agents/research/.env` |
| `CLICKUP_TOKEN`, `CLICKUP_TEAM_ID` | `agents/pm/.env` |
| `GITHUB_TOKEN` | `agents/coding/.env` |
| `KOLA_API_KEY` | `agents/kola/.env` |
| `N8N_API_KEY`, `N8N_WEBHOOK_SECRET` | `agents/n8n/.env` |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY` | one copy per agent, by design |

The model-provider keys are deliberately duplicated per agent: a shared file
would defeat per-agent isolation, which is what stops the research agent
reaching a ClickUp token. Nothing else is duplicated, and one path matters more
than the rest:

> **Hermes' own `.env` is `%LOCALAPPDATA%\hermes\.env`.** Not `~/.hermes` —
> that path appeared in this repository's own rotation checklist until
> 2026-09-23, a rotation followed it, all six agent files were updated and
> Hermes' was not. The gateway came up with a revoked bot token and Telegram
> went silent. `check_keys.py` reads the real path and would have caught it in
> one command.

`GEMINI_API_KEY` was also once kept *only* as a User environment variable. A
scheduled task does not inherit the interactive user's environment, so the
gateway had no key while `hermes` in a terminal worked — the file copy is the
one that counts now, and an environment copy is optional.

**To rotate:** edit the `.env` in an editor. Never paste a key into a terminal
or a chat — it lands in scrollback and transcripts. Then:

```bash
python hermes/check_keys.py     # one authenticated call per key; names only
hermes gateway restart
```

`check_keys.py` prints the key **name**, its length and valid/invalid. Never a
value. A `429` counts as valid — the credential authenticated, the account is
out of quota.

### state.db is a credential-bearing file

**Hermes' `%LOCALAPPDATA%\hermes\state.db` stores credentials in plaintext, by
design.** It keeps every message, tool call, tool result and reasoning trace
verbatim, so any key that appears in a session lands there. That includes a
key pasted into a chat, a `curl -H "Authorization: …"` an agent ran, or an
`.env` an agent read. A rotated key reappears there within a few sessions of
being used. On 2026-10-02 a sweep found the live `TELEGRAM_BOT_TOKEN` and
`DEEPSEEK_API_KEY`, two Airtable PATs, and old OpenRouter, ClickUp and GitHub
keys. The same goes for `sessions/request_dump_*.json`.

Treat `state.db`, its `-wal`, the `sessions/` folder and every copy of them as
secrets for **backup and disposal**:

- never sync, upload or attach them; any backup of them is a credential store
- dispose of an old copy as you would a `.env`, not as a log
- scrubbing is a one-off clean-up, not a control: redact, then rebuild the
  search indexes and VACUUM (`hermes sessions optimize`), or the old pages and
  FTS segments still hold the text. Rotation is the control.
- a cron run's `session_search` can read this store (AUDIT-2 section 17)

If `N8N_WEBHOOK_SECRET` changes, the n8n workflow's Code node holds the same
value and must change with it, and `docker restart n8n-receiver`.

---

## 7. Model chain, cost and quota

| tier | provider | model | what runs out |
|---|---|---|---|
| only | DeepSeek (paid) | `deepseek-flash` (DeepSeek-V4.1-Flash, per `GET /models`) | the balance |

**No fallback, on purpose (2026-10-02).** When DeepSeek is down or the balance
is spent, Hermes stops answering. It does not hand the conversation to a free
tier that behaves differently. `restore` writes `fallback_providers: []`,
removes any legacy `fallback_model`, and fails unless `GET /models` still lists
the model. Startup guard condition 9 refuses to start the gateway if Hermes'
own merged fallback chain is non-empty. The paused client-engine cron jobs keep
their own per-job Gemini setting, which is not this chain.

Groq stays out of Hermes' chain: its 8,000 tokens/minute cannot fit one routing
turn. It still serves the agents, from their own `.env` files.

Set and verified by one command, which `post_update.py` also runs:

```bash
python hermes/configure_providers.py restore
```

### Cost, measured

Three routing turns through `tools/logging_proxy.py`, priced at deepseek-flash
rates (per 1M tokens: cache hit $0.003 / miss $0.15 / output $0.60 off-peak;
double at peak, 01:00–04:00 and 06:00–10:00 UTC on weekdays):

| turn | calls | prompt tokens/call | latency/call | off-peak | peak |
|---|---|---|---|---|---|
| research | 2 (+1 title) | 9,113 → 9,963 | 1.3–1.4s | $0.0011 | $0.0023 |
| n8n list | 2 (+1 title) | 9,118 → 9,357 | 1.2s | $0.0010 | $0.0019 |
| research + kola | 3 (+1 title) | 9,139 → 11,123 | 1.4–2.3s | $0.0007 | $0.0014 |

No 4xx on any routing call. The one 400 per new session is the title helper
asking for `response_format: json_schema`, which DeepSeek does not offer; Hermes
retries without it and gets a 200.

### Who runs on what

| job | schedule | model | sampled cost per run (off-peak / peak) |
|---|---|---|---|
| Telegram routing turn | on demand | DeepSeek `deepseek-flash` | $0.0007–0.0011 / $0.0014–0.0023 |
| W2 daily briefing | weekdays 07:30 | DeepSeek | $0.0005 / $0.0009 |
| W3 weekly review | Mondays 08:00 | DeepSeek | $0.0011 / $0.0022 |
| followup-monitor (**paused**) | weekdays 09:00 | DeepSeek | $0.0005 / $0.0010 |
| client-coordinator (**paused**) | every 30 min | Gemini `gemini-3.1-flash-lite` (free) | $0 — ~94k input tokens |
| reply-processor (**paused**) | every 60 min | Gemini `gemini-3.1-flash-lite` (free) | $0 — ~124k input tokens |
| n8n notice drain | every minute | none (script) | $0 |

Every figure is one real run on 2026-09-30, priced from the tokens Hermes
recorded for that session, not estimated. A job's own `provider`/`model`
(`hermes cron edit <id> --provider gemini --model …`) routes it; verified in
the session record (`billing_provider: gemini`).

**A normal day on DeepSeek: about $0.03–0.08** — 30 Telegram turns plus the
three scheduled jobs (~$0.001). The Hermes **desktop app** uses the same config
and is the one large, variable item: one long desktop session on 2026-09-30 was
38 calls and $0.13 on its own.

**The client-engine jobs ran on Gemini** (per-job provider), at ~100k tokens a
run: `gemini-3.1-flash-lite`'s 250k/day lasted about two runs. The global chain
is now empty, so a job like this has nothing to fall to.

### client-coordinator and reply-processor: PAUSED 2026-10-02

Paused, not deleted (`hermes cron pause 16ec4edab510`, `… 96185b3dcf0d`;
`hermes cron resume <id>` undoes it). They have never done their job once:

- **Zero successful runs.** 59 recorded sessions (37 + 22) and 177 execution
  attempts up to 2026-10-02 07:39. Every one ended `[SILENT]`, `[CRON_FAILURE]`
  or with an "I cannot" answer. **Not one Airtable or Gmail call**, because the
  cron tool set has neither. The runs spent their turns on
  `n8n list_workflows`, `github_query`, `kola_catalogue` and `pm_query` instead.
- **Every write is refused, by design.** No human is present in a cron run to
  answer the approval card, so the gate refuses any write instantly. The runs
  tried five: `start_code_task` twice (once pointed at the Hermes home, to
  "list the Airtable bases using curl"), `run_workflow` twice and
  `github_action` once. All five came back "The user did not approve". A
  working version would hit the same refusal on every Airtable update it exists
  to make.
- **They cost quota.** About 100k input tokens a run.

Do not resume either job until the client-engine runs as a script with
pre-approved, narrowly scoped writes, not as a cron agent. A cron agent cannot
get its writes approved, so one that exists to write cannot work.
`followup-monitor` (`2feac44c0733`, same skills, same missing tools) was
paused too, on 2026-10-02. It had not tried a write yet; it would have.

The client engine is now **on demand only**: "check client replies" in
Telegram (section 1). Two of these cron runs also tried to start coding tasks
to curl Airtable; see AUDIT-2 section 17.

### Watching the balance

```bash
curl -s -H "Authorization: Bearer $DEEPSEEK_API_KEY" https://api.deepseek.com/user/balance
```

When the balance is spent DeepSeek refuses and Hermes stops answering until you
top up. The table below is what each workflow costs in tokens:

| workflow | Hermes calls | input tokens | runs/day on Gemini (historical) |
|---|---|---|---|
| simple read | 2 | ~13,200 | ~19 |
| W2 daily briefing | 2 | **16,625** | ~15 |
| W1 proposal | 4–6 | ~50,000 | ~5 |
| W4 code task | 10+ | ~100,000 | ~2 |

Options when spend or quota is the problem:

1. **Pause a scheduled job** with `hermes cron pause <id>`. The client-engine
   jobs are the largest consumer of free quota by far.
2. **Narrow a prompt.** W2 went from 63,000 to 16,625 tokens purely by asking
   for one thing instead of surveying the workspace.
3. **Top up DeepSeek** at platform.deepseek.com/top_up.

Do not raise `max_turns` or add polling to work around a limit; it spends the
remainder faster.

---

### Airtable (remote MCP server)

Installed 2026-09-24 06:49 from Hermes' bundled catalog
(`hermes-agent/optional-mcps/airtable`) by a desktop-app suggestion pill, which
appears whenever "airtable" is typed or spoken. OAuth consent was completed in
the browser 28 seconds later, with full read and write scope.

Registered like an agent: `trust: untrusted`, nine of its 46 tools included
(six reads; `create_records_for_table`, `update_records_for_table` and
`create_record_comment` as writes that require approval), resources and prompts
off. No delete, schema, automation or `list_secrets` tool is included. It is
**not** on the Telegram or cron surface. Adding it to either is a decision,
not a fix. Defined in `hermes/configure_orchestrator.py` (`REMOTE_SPEC`) and the
manifest.

To revoke the grant: airtable.com → Account → Integrations → third-party
integrations → Hermes, then delete
`%LOCALAPPDATA%\hermes\mcp-tokens\airtable*.json`.

---

## 8. Where things live

| what | where |
|---|---|
| Hermes logs | `%LOCALAPPDATA%\hermes\logs\` — `gateway.log`, `agent.log`, `errors.log` |
| guard verdicts | `%LOCALAPPDATA%\hermes\logs\gateway-guard.log` |
| cron output (incl. silent runs) | `%LOCALAPPDATA%\hermes\cron\output\<job id>\` |
| per-agent audit | `logs/<agent>/<agent>-YYYY-MM-DD.jsonl` — every tool call |
| proposals | `agents/docs/output/*.pdf` (gitignored) |
| n8n queue | `agents/n8n/queue.jsonl` (gitignored) |
| measurements | `evidence/` (gitignored) |
| config | `%LOCALAPPDATA%\hermes\config.yaml` — rebuilt by the hermes/ scripts |

**Cron deliveries are logged to `agent.log`, not `gateway.log`.** Looking in
the wrong one made a successful delivery look like a failure once.

The per-agent JSONL is the honest record: it shows which tool ran, with which
argument **keys**, and what came back. When a reply and a log disagree, the log
is right.

---

## 9. Routine checks

```bash
python hermes/check_keys.py                     # credentials still alive
python hermes/scan_stale_credentials.py         # credential copies nobody is managing
python hermes/verify_agents.py                  # all six agents boot and match the manifest
python hermes/redact_docs.py --check            # nothing leaked into documentation/
python hermes/gateway_launcher.py --check       # the guard, without starting anything
hermes cron list                                # jobs, schedules, last run
```

`scan_stale_credentials.py` asks the question `check_keys.py` cannot: **what
credentials exist on this disk that nothing is tracking?** Rotation only helps
for copies you know about, and every tool that edits a config takes a backup
first. It matched nine such files on 2026-09-23 — five 2026-09-08 configs, a
"known good" config, a 27 KB pre-rebuild `.env`, its `config.yaml`, and a
curator blob holding four ClickUp tokens — every one of them a rotated-out
value, none of them covered by the approval patch. `--delete-stale` removes only
files whose every match is already dead; anything still live is named, never
swept.

It reports **names, lengths and digests, never values**, and it self-checks:
if a key in a managed `.env` looks like a credential and matches no pattern, it
says so, because an unrecognised shape reads exactly like a clean disk. That
check found two of its own blind spots on first run. `--self-test` plants a
credential-shaped string that was never issued and requires the sweep to find
it.

`verify_agents.py` asks the **agents**, where the guard asks Hermes. It spawns
each server over stdio exactly as Hermes does and compares what the process
publishes — tool names both ways, and the `readOnlyHint` on each one — against
the manifest. That second check is the approval gate itself: a write tool that
gained the annotation would silently stop asking. Where a read tool is free it
is called for real; where it would spend model quota the line says `schema
only`, rather than claiming more than was checked.

Before shipping a change that touches the invariants — the tool-surface
allowlist, the startup guard, the approval patch, per-agent `.env` isolation,
the coding sandbox — re-run the **negative** tests, not the positive check:

```bash
agents/.venv/Scripts/python tests/test_surface_guard.py
```
