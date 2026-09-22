# Upstream: `readOnlyHint` is never read, so the MCP trust gate blocks every tool

**Component:** `tools/mcp_tool_registration.py:_annotation_read_only_hint()`
**Affects:** every MCP server configured `trust: untrusted`
**Severity:** fails closed, so nothing becomes unsafe — but the feature it gates
is unusable, and unattended runs cannot proceed at all.
**Found:** 2026-09-22, against Hermes with `mcp` SDK 2.x.

## What happens

`_trust_gate_check()` requires human approval for any tool on an `untrusted`
server whose `readOnlyHint` is not exactly `True`. The hint is captured at
discovery by:

```python
def _annotation_read_only_hint(mcp_tool: Any) -> bool:
    annotations = getattr(mcp_tool, "annotations", None)
    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) \
           else getattr(annotations, "readOnlyHint", None)
    return hint is True
```

The SDK returns `annotations` as a **pydantic `ToolAnnotations` model**, whose
Python attribute is `read_only_hint`. `readOnlyHint` exists only as the
serialization alias. So `getattr(annotations, "readOnlyHint", None)` is always
`None`, and every tool is classified write-capable.

## Measured

Against a live MCP server that publishes `readOnlyHint: true`, run under
Hermes' own interpreter:

```
tool: pm_query
  annotations object   : ToolAnnotations(title=None, read_only_hint=True, ...)
  read_only_hint       : True
  model_dump()         : {'read_only_hint': True, ...}
  model_dump(by_alias) : {'readOnlyHint': True, ...}
  Hermes verdict       : _annotation_read_only_hint -> False     # wrong
```

The wire is correct — a raw JSON-RPC client reading `tools/list` sees
`"annotations": {"readOnlyHint": true}`. Only the Python-side read is wrong.

## Consequence

A scheduled job calling a read-only tool fails, with no way to recover:

```
Tool mcp__pm__pm_query returned error:
  {"error": "The user did not approve running write-capable MCP tool 'pm_query'
   on untrusted server 'pm'. The command was NOT run."}
```

There is nobody to approve an unattended run, so `trust: untrusted` and cron are
mutually exclusive in practice — which defeats the purpose of annotating read
tools as read-only.

The discovery cache (`cache/mcp_schema_cache.json`) stores the **computed**
hint, so a cache written while this is live records `readOnlyHint: false` for
every tool and survives a fix until it is cleared.

## Suggested fix

Accept both spellings, for the SDK object and the cache dict. `unknown →
write-capable` stays, so the fail-closed default is unchanged:

```python
if isinstance(annotations, dict):
    hint = annotations.get("readOnlyHint")
    if hint is None:
        hint = annotations.get("read_only_hint")
else:
    hint = getattr(annotations, "readOnlyHint", None)
    if hint is None:
        hint = getattr(annotations, "read_only_hint", None)
```

Better still, read it the way the SDK intends and let pydantic resolve the
alias: `annotations.model_dump(by_alias=True).get("readOnlyHint")`.

A second, separate issue: the schema cache's fingerprint does not appear to
cover annotations, so a server that *changes* a tool's `readOnlyHint` — a
security-relevant change — does not invalidate the cached entry.

## Applied locally

`hermes/patch_readonly_hint.py` — marker-guarded, idempotent, re-applied by
`hermes/post_update.py`, and it clears the stale schema cache. Guard condition 8
(`hermes/check_telegram_surface.py`) now compares Hermes' own computed hints
against `hermes/surface-manifest.json` on every gateway start, so a regression
here refuses the start instead of silently disarming approvals.
