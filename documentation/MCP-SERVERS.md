# Third-party MCP servers

One row per connected server, with where it attaches and why.

| server | owner | transport | pinned to | licence | status |
|---|---|---|---|---|---|
| `github/github-mcp-server` | coding agent | stdio (Docker) | `v1.12.2`, digest `sha256:508a0857…cecac6` | MIT | **live** |
| `@upstash/context7-mcp` | coding agent | stdio (node) | `4.1.1` exactly (see caveat) | MIT | **live** |
| Kolaborate (`mcp.kolaborate.africa`) | pending | streamable HTTP | — hosted, no version | — none published | **blocked: needs an API key** |

Nothing in this table is attached to Hermes. Every one is connected *by an
agent*, which applies its own allowlist and exposes only its own tools.

## Why none of them attach to Hermes directly

The placement rule allows a read-only, low-risk server to attach to Hermes. In
practice none of these qualify, for two separate reasons, and the second one
binds even when the first does not.

**Capability.** GitHub can create branches, commit and open pull requests.
Kolaborate can post to jobs, chats and applications. Both are write-capable, so
both go behind an agent.

**Prompt budget.** Hermes' whole per-call prompt is **6,660 tokens** measured on
the current surface, against a free tier of 250,000 input tokens per *day* — so
roughly 19 turns a day at 2 calls each. Tool schemas cost about 300 tokens each
on this surface. Attaching directly would mean:

| server | tools | est. added tokens/call | new per-call total |
|---|---|---|---|
| GitHub (`repos,pull_requests,context`) | 32 | ~9,600 | ~16,300 |
| Context7 | 2 | ~600 | ~7,300 |
| Kolaborate | 122 | ~36,600 | ~43,300 |

The ceiling in the placement rule is 9,000. GitHub and Kolaborate blow through
it on their own; Kolaborate alone would cost more than six times Hermes' entire
current prompt, on every call, and exhaust a day's quota in about six turns.

Behind an agent, all three cost Hermes exactly what its own tools cost: three
schemas (`github_query`, `github_action`, `docs_query`), which measured **+80
tokens per call** in total.

## What Hermes actually sees

```
Hermes ──MCP──▶ coding agent ──MCP client──▶ github-mcp-server   (32 tools, 13 allowed)
                             └─MCP client──▶ context7            ( 2 tools,  2 allowed)
```

| Hermes sees | what it reaches | approval |
|---|---|---|
| `github_query` | 9 read tools | none — `readOnlyHint: true` |
| `github_action` | those 9 + 4 write tools | **required** |
| `docs_query` | `resolve-library-id`, `query-docs` | none — `readOnlyHint: true` |

## GitHub: the allowlist

Enforced in `agents/coding/main.py` on *our* side of the wire, so a server that
publishes a different tool list later gains nothing. `GITHUB_TOOLSETS` narrows
the server as well, which is defence in depth and not the control.

**Read (9)** — `get_me`, `get_file_contents`, `list_branches`, `list_commits`,
`get_commit`, `list_pull_requests`, `pull_request_read`, `search_code`,
`search_repositories`

**Write (4)** — `create_branch`, `create_or_update_file`, `push_files`,
`create_pull_request`

**Excluded on purpose (12)**, all of which exist at v1.12.2:
`merge_pull_request`, `delete_file`, `create_repository`, `fork_repository`,
`update_pull_request`, `update_pull_request_branch`, `pull_request_review_write`,
`add_comment_to_pending_review`, `add_reply_to_pull_request_comment`,
`get_teams`, `get_team_members`, `list_repository_collaborators`.

There is no `actions` toolset enabled at all, so workflow tools do not exist in
that process — which is stronger than excluding them by name.

`GITHUB_TOKEN` lives in `agents/coding/.env` and nowhere else. It is handed to
the container through `docker run -e NAME` (by name, not by value), so it does
not appear in an argv that `docker ps` or the process list would show.

### Verified

```
7 tools exposed to Hermes:
  RO  docs_query     RO  get_result     RO  get_status
  RW  github_action  RO  github_query   RO  list_changed_files
  RW  start_code_task
OK: no downstream GitHub/Context7 tool is on the Hermes surface
```

A real read through the chain returned the default branch and the two most
recent commit messages of this repository, correctly, including the commit made
earlier the same day.

## Context7: how strong the pin actually is

`agents/coding/vendor/package.json` names `"4.1.1"` exactly, not `^4.1.1`, so
`npm install` resolves that version and no other. The installed tree also has
the integrity hash `sha512-fUARTIZ…C5pA==`.

**This was weaker than it looked until 2026-09-23.** `package-lock.json` was
gitignored, so a fresh clone was pinned by *version*, not by *hash*: it would
fetch 4.1.1, but nothing in the repository proved the tarball was byte-identical
to the one running here — and Pi, asked for as `^0.86.1`, was not even pinned by
version.

**The lockfile is now committed**, and `npm ci` installs from it, so a fresh
clone resolves one exact tree: 252 packages, 246 of them carrying a `sha512`
integrity hash, including this one. The sandbox image builds from the *same*
lockfile rather than `npm install -g`, so host and container agree by
construction instead of by a comment saying they do.

The five packages with no integrity hash are all `@earendil-works/*` 0.86.1 —
Pi's own sub-packages. They are pinned to an exact version and an exact
`resolved` URL, which is what npm recorded for them; the registry does not
publish hashes this lockfile can carry. That is the remaining gap, it is
upstream's to close, and it is narrower than "any 0.86.x".

## Context7: the rename that broke the first run

The first live `docs_query` failed:

```
HTTP 400: Tool call validation failed: attempted to call tool
'get-library-docs' which was not in request.tools
```

`get-library-docs` is what Context7 called it before 4.x; 4.1.1 calls it
`query-docs`. The allowlist named the old one, so the intersection dropped it
silently, and the model — which knows the old name perfectly well from training
data — called it anyway.

The allowlist was wrong, but the *silence* was the defect. `Downstream.available()`
now writes an `mcp_allowlist_stale` audit line naming any allowlisted tool the
server does not offer, so the next rename points at the config line that moved
instead of at the model.

## Write approval

**Hermes supports this natively and no plugin was needed.** The prompt's
fallback design — pending action ids, `/approve <id>`, `/reject <id>` — is not
built, because building it would have added a second, weaker approval path
beside a working one.

`mcp_servers.<name>.trust: untrusted` in `config.yaml` arms Hermes' own gate
(`tools/mcp_tool_handlers.py:_trust_gate_check`). On an untrusted server, any
tool whose discovery-time `readOnlyHint` is not exactly `True` must be approved
by a human **before the RPC is sent**. It is enforced in Hermes' MCP layer; the
model is not consulted and cannot skip it.

All three servers are `trust: untrusted`. Which tools that catches is decided by
the annotations the agents publish, so the three write tools — `pm_action`,
`start_code_task`, `github_action` — are deliberately *not* annotated, and every
read tool is.

Both halves are checked. Guard condition 7 fails the gateway start if any server
is not `trust: untrusted`; `tests/test_surface_guard.py` starts all three agents
over real MCP stdio and asserts each tool's annotation matches
`surface-manifest.json`'s `write_tools` list — because a write tool that *gained*
`readOnlyHint: true` would lose its prompt while every static check still passed.

### Verified, by calling the gate directly

Under Hermes' own interpreter, with no model involved:

| case | result |
|---|---|
| untrusted + `pm_action` (write) | **BLOCKED** |
| untrusted + `pm_query` (`readOnlyHint: true`) | allowed |
| untrusted + unknown tool (no hint) | **BLOCKED** |
| untrusted + hint is the *string* `"true"` | **BLOCKED** |
| `trust: full` + `pm_action` | allowed — the gate is disarmed |

The last row is the reason guard condition 7 exists: `full` is Hermes' compat
default, so a hand-edited or regenerated `mcp_servers` block arrives with
approval off and nothing says so.

Fail-closed confirmed: with no human channel available the gate denied rather
than waiting or passing through.

**Still to verify:** that the approval prompt renders and can be answered *on
Telegram*. The code path is `_is_gateway_approval_context()` →
`_await_gateway_decision`, which is the same round-trip the dangerous-command
gate already uses, but it has not been exercised end to end from a phone.

## Kolaborate — key received, service down, analysis corrected

**Correction.** The table above and the placement arithmetic earlier in this
file said Kolaborate exposes **122 tools** costing ~36,600 tokens per call.
That came from its landing page. With the key in hand, its real `tools/list`
returns **two**:

```
  kola_discover   Search the Kola tool catalog (filtered to your access level).
  kola_call       Execute any Kola tool by exact name.
```

It is a meta-dispatcher, not a 122-tool surface. Attached directly to Hermes it
would cost roughly 600 tokens, not 36,600 — so the token argument for keeping it
behind an agent **does not hold**, and I withdraw it.

The placement does not change, for a better reason. `kola_call` is a single
generic entry point that can invoke any of the 122 operations, including writes,
with the operation named in its *arguments*. That defeats tool-level policy
entirely:

- Hermes' allowlist works on tool NAMES, so it can only allow `kola_call` or
  ban it. "Jobs read but not jobs write" is inexpressible.
- Hermes' write-approval gate keys on a per-tool `readOnlyHint`. `kola_call` has
  none, so every call prompts — including reads — and the prompt cannot say
  which of the 122 operations is about to run.

An owning agent can enforce what Hermes cannot: an allowlist on the **inner
operation name** inside `kola_call`'s arguments, and a read/write split that
maps onto two agent tools. So it goes behind an agent because that is the only
place the policy is expressible, not because of token cost.

### Blocked: the service is down

The key works — `tools/list` returned the two tools above at 14:18. Ten minutes
later every call, including `tools/list` with the same key, returned:

```
{"error":"Authentication service unavailable."}
```

Consistently, across four attempts and both the SDK and raw HTTP. Meanwhile:

```
GET /api/health -> {"ok":true,"service":"kola-mcp-server",
                    "checks":{"convexUrlConfigured":true,"convexUrlMatchesExpected":true}}
```

Their health endpoint checks that a Convex URL is *configured*, not that it is
*reachable*, so it reports healthy while authentication is down. It is not a
liveness signal and should not be treated as one.

`agents/kola/.env` holds the key (gitignored) and `agents/kola/discover.py` will
enumerate the catalogue the moment the service returns.

### Also fixed on the way

The HTTP transport in `_common/mcp_client.py` had never been exercised — GitHub
and Context7 are both stdio — and it imported `httpx`. The SDK vendors its
client as `httpx2`, so the first real HTTP connection raised `ModuleNotFoundError`.
It now uses the SDK's own `create_mcp_http_client`, which avoids naming the
module at all.

## Kolaborate — original provenance notes

Added at your request mid-run. It is **not wired up**, because it needs an API
key that only you can mint.

| | |
|---|---|
| endpoint | `https://mcp.kolaborate.africa/api/mcp`, streamable HTTP |
| auth | `Authorization: Bearer kola_live_…`, minted from their dashboard |
| tools | 122, across Testing (65), Internships (15), Marketplace/Jobs (14), Hackathons (7), Academy (6), Chat (3), Careers (2), Kolaborators (1), Profile (1) |
| health | `{"ok":true,"service":"kola-mcp-server"}`; live |
| org | `github.com/Kolaborate-Platforms`, 9 public repos |

Step 1 of the procedure is "confirm it is official or actively maintained, pin an
exact version or commit, record the license", and it **cannot be completed as
written**:

- It is a **hosted service**, not a package. There is no version to pin and no
  commit to record — the endpoint can change under us at any time, with no
  release note and no way to detect it except behaviour.
- **The MCP server is not in a public repository.** The org's nearest published
  code is `kola-mail-mcp` (MIT, last pushed 2026-02-26), which is a different
  service. The org's most recent public push of anything is 2026-05-31.
- **No licence is published** for the hosted endpoint.

That is not a reason to refuse it — it is first-party for a platform you use,
and plenty of useful MCP servers are hosted. It is a reason to place it the
strict way rather than the convenient one, which I would recommend regardless of
the key:

1. **Behind an agent**, not on Hermes. 122 tools would cost ~36,600 tokens per
   call against a 250,000/day budget.
2. **A narrow allowlist**, chosen from the real tool list once the key exists —
   not all 122. I would expect Marketplace/Jobs read plus Chat read to be the
   useful core.
3. **Read and write split** into separate agent tools, so the write half is
   gated by the same `trust: untrusted` mechanism as everything else.
4. Its responses fenced as untrusted — they carry text written by other users of
   a marketplace, which is exactly the injection surface `_common/guard.py`
   exists for.

The client side is already built and transport-ready: `_common/mcp_client.HttpSpec`
carries a bearer header and `Downstream` treats an HTTP server identically to a
stdio one. What is missing is the key, the tool list it unlocks, and a decision
about which agent owns it.
