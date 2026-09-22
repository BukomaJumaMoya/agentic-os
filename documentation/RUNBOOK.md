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

Scheduled, unattended:

| job | when | behaviour |
|---|---|---|
| W2 daily briefing | 07:30 Mon–Fri | overdue + due today; **silent if nothing** |
| W3 weekly review | 08:00 Monday | completed, slipped, stale PRs; silent if nothing |
| n8n notice drain | every minute | delivers queued n8n notices; silent when the queue is empty |

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
3. model chain (and the reasoning-effort trap)
4. approval patch — secret-store reads require approval
5. `readOnlyHint` alias patch — without it every read tool is gated
6. elicitation per-call patch — without it Telegram offers "Always Allow"
7. gateway launcher + scheduled-task policy
8. the startup guard, all eight conditions
9. the guard's negative tests

Anything but `PASS` at the end means do not start the gateway.

---

## 4. Reading a guard refusal

The gateway refuses to start when any of eight conditions fails. You will know
because **Telegram goes quiet** — and because the launcher sends you a
fixed-text alert saying so.

```
1  tool surface        nothing outside the manifest on the Telegram surface
2  approval patch      reading ~/.hermes/.env still asks
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
| `GEMINI_API_KEY` | `~/.hermes/.env` **and** the User env var |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` | `~/.hermes/.env` |
| `TAVILY_API_KEY` | `agents/research/.env` |
| `CLICKUP_TOKEN`, `CLICKUP_TEAM_ID` | `agents/pm/.env` |
| `GITHUB_TOKEN` | `agents/coding/.env` |
| `KOLA_API_KEY` | `agents/kola/.env` |
| `N8N_API_KEY`, `N8N_WEBHOOK_SECRET` | `agents/n8n/.env` |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY` | one copy per agent, by design |

The model-provider keys are deliberately duplicated per agent: a shared file
would defeat per-agent isolation, which is what stops the research agent
reaching a ClickUp token.

**To rotate:** edit the `.env` in an editor. Never paste a key into a terminal
or a chat — it lands in scrollback and transcripts. Then:

```bash
python hermes/check_keys.py     # one authenticated call per key; names only
hermes gateway restart
```

`check_keys.py` prints the key **name**, its length and valid/invalid. Never a
value. A `429` counts as valid — the credential authenticated, the account is
out of quota.

If `N8N_WEBHOOK_SECRET` changes, the n8n workflow's Code node holds the same
value and must change with it, and `docker restart n8n-receiver`.

---

## 7. Quota — measured, and it binds

Free tiers: **Gemini 250,000 input tokens per day, per model** (two models in
the chain), **OpenRouter 50 requests/day account-wide**, **Groq 8,000
tokens/minute** for the agents.

| workflow | Hermes calls | input tokens | runs/day on the primary |
|---|---|---|---|
| simple read | 2 | ~13,200 | ~19 |
| W2 daily briefing | 2 | **16,625** | ~15 |
| W1 proposal | 4–6 | ~50,000 | ~5 |
| W4 code task | 10+ | ~100,000 | ~2 |

Roughly double those before the chain falls to OpenRouter, which then gives
about a dozen more turns before it too is spent for the day.

### When a quota is hit

You will see replies arriving from `nvidia/nemotron-3-ultra…` instead of
`gemini-*`, or a `RESOURCE_EXHAUSTED` in the logs.

```bash
grep -c "provider=gemini" %LOCALAPPDATA%\hermes\logs\agent.log
grep "RESOURCE_EXHAUSTED" %LOCALAPPDATA%\hermes\logs\errors.log | tail -3
```

Options, in order of how much they cost you:

1. **Wait.** The per-model cap resets at 00:00 UTC.
2. **Pause a scheduled job** — `hermes cron pause <id>`. W2 costs about one
   proposal a day.
3. **Narrow a prompt.** W2 went from 63,000 to 16,625 tokens purely by asking
   for one thing instead of surveying the workspace.
4. **Pay for Gemini.** The only option that removes the ceiling.

Do not raise `max_turns` or add polling to work around a quota; it spends the
remainder faster.

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
python hermes/check_keys.py               # credentials still alive
python hermes/redact_docs.py --check      # nothing leaked into documentation/
python hermes/gateway_launcher.py --check # the guard, without starting anything
hermes cron list                          # jobs, schedules, last run
```

Before shipping a change that touches the invariants — the tool-surface
allowlist, the startup guard, the approval patch, per-agent `.env` isolation,
the coding sandbox — re-run the **negative** tests, not the positive check:

```bash
agents/.venv/Scripts/python tests/test_surface_guard.py
```
