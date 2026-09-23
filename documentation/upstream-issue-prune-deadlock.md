# Upstream: proactive tool-result pruning can only run after a tool call, so a session that has stopped calling tools can never be pruned

**Component:** `agent/turn_preflight.py:369` — the sole call site of
`ContextCompressor.prune_tool_results_only()`
**Affects:** any long-running session with `compression.proactive_prune_tokens`
set, on a model whose context window makes the ratio threshold unreachable.
**Severity:** the feature silently does nothing in exactly the situation it
exists for. No warning is logged, because the code path is never entered.
**Found:** 2026-09-23, Hermes Agent v0.21.3 (upstream `a782e2ee7`), against
`gemini-3.5-flash-lite` (1,048,576-token window) over the Telegram gateway.

## What happens

`prune_tool_results_only()` is a deterministic, LLM-free pass that summarises
old tool results to reclaim context. It has **one call site in the codebase**:

```python
# agent/turn_preflight.py:369, inside compress_after_tool_results()
_prune = getattr(_compressor, "prune_tool_results_only", None)
if callable(_prune):
    _pruned_msgs, _pruned_n = _prune(messages, current_tokens=_real_tokens)
```

`compress_after_tool_results()` is defined at `turn_preflight.py:243` and is
imported by `agent/turn_tool_round.py` — it runs **after a tool round returns
results**.

A turn that makes no tool call never enters a tool round, so the prune is never
reached. That is fine until the context grows large enough to change the model's
behaviour, at which point the first thing to degrade is **tool calling**. From
then on:

> the mechanism that relieves context pressure only runs after a tool call, and
> the failure caused by context pressure is that the model stops calling tools.

A session can only be pruned while it is healthy. It becomes unprunable at
precisely the moment pruning is needed, and it cannot recover on its own.

The other compression site, `run_preflight_compression()`, *does* run before
every model call — but it is gated on `should_compress()`, i.e.
`threshold × context_length`. With the default `threshold: 0.5` and a
1,048,576-token window that is **524,288 tokens**, which a 72,000-token turn
never approaches. So on a large-window model the only reachable relief valve is
the one behind the tool round.

## Measured

A Telegram session with 243 active messages, ~71,100 billed input tokens, and
46 fenced tool results. Two read questions that had previously routed correctly
in a fresh session:

```
config: proactive_prune_tokens: 60000   (raised from 0)
        proactive_prune_min_result_chars: 8000
        protect_last_n: 20
        threshold: 0.5            -> threshold_tokens = 524,288

22:29:13  inbound: 'What does the httpx AsyncClient timeout parameter do? Check the docs.'
22:29:18  API call #1  in=72555  ->  Turn ended  api_calls=1  response=712 chars
22:29:25  inbound: 'In <owner>/<repo>, what are the last 3 commits on master?'
22:29:27  API call #2  in=72787  ->  Turn ended  api_calls=1  response=110 chars
```

Neither turn called a tool. Both replies were **character-identical** to the
same questions asked before pruning was enabled — the same 712-character answer
from the model's own weights, and the same sentence declining to read a
repository the agent has a working tool and a valid token for.

Session state before the config change and after two turns under it:

| | before | after |
|---|---|---|
| active messages | 243 | 247 |
| estimated tokens | 65,370 | 65,651 |
| tool results | 82 | 82 |
| **fenced tool results** | **46** | **46** |
| billed input on the failing turn | ~71,100 | **72,555** |

Nothing was pruned. The context grew.

**The prune itself is not broken.** Driving the same transcript through
`_prune_old_tool_results()` directly, with the live configuration:

```
BEFORE   243 messages   65,370 tokens   192,416 chars
AFTER    243 messages   49,247 tokens   128,652 chars   7 messages summarised
         -> 16,123 tokens reclaimed, -24%
```

It works, reclaims a quarter of the context, and never runs.

## Consequence

1. **The feature is unreachable on the path that needs it.** Setting
   `proactive_prune_tokens` on a large-window model appears to take effect and
   changes nothing, with no log line to say so — the early return happens
   before any of the `_warn_reclamation_no_op()` sites.
2. **The failure is self-reinforcing.** Each degraded turn appends a user
   message and an assistant message and calls no tool, so the context grows
   monotonically and the prune stays out of reach. The session cannot recover.
3. **It is silent.** The operator sees a plausible answer and a confident
   refusal, not an error. Detecting it required reading `in=` token counts
   across turns and noticing that two replies were byte-identical.

## Suggested fix

Call the prune from the **pre-API** site as well, gated on its own
`proactive_prune_tokens` rather than on the compression threshold. The function
already returns the input object unchanged when nothing is eligible, so the
no-op contract at the new site is the same one `compress_after_tool_results()`
relies on:

```python
# agent/turn_preflight.py, in run_preflight_compression(), alongside the
# existing should_compress() branch
elif agent.compression_enabled:
    _prune = getattr(_compressor, "prune_tool_results_only", None)
    if callable(_prune):
        _pruned, _n = _prune(messages, current_tokens=_real_tokens)
        if _n and _pruned is not messages:
            messages = _pruned
```

The prune is deterministic and LLM-free, so running it before a model call
costs no provider request — unlike full compaction, which is why it is gated
separately in the first place.

Two smaller things would have made this findable:

- **log the early return.** The `proactive_prune_tokens <= 0 or current_tokens <
  threshold` guard returns without a `_warn_reclamation_no_op()` call, so "not
  yet" and "never, structurally" look identical from the outside.
- **warn when `threshold_tokens` is unset on a large-window model.** A ratio
  threshold of 0.5 against a 1M-token window is 524,288 tokens; sessions that
  degrade at a tenth of that will never reach any compression site.

## Worked around locally

`proactive_prune_tokens: 60000` is kept, because where it *does* run — a session
still calling tools — it reclaims 24% for no quota, and
`archive_and_compact()` soft-archives the originals rather than destroying them.

The routing failure itself is handled operationally instead: one job per
session, `/new` in between. A fresh session routes correctly every time
(`docs_query` and `github_query` both called, `in=8421` versus `in=72555`).
`compression.threshold_tokens: 60000` would also reach the failing path, but it
triggers LLM compaction — which spends provider quota and rewrites the
transcript that the next investigation would need.
