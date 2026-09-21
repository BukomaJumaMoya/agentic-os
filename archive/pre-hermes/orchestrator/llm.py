#!/usr/bin/env python3
"""
LLM boundary -- the single place the orchestrator talks to a model.

Isolated here so that:
  - flagship.py holds no HTTP or provider logic
  - tests stub one function (complete) rather than patching transport
  - the provider, model id and prompt version are recorded for every call

PROVIDER CHAIN
--------------
Every free tier in use here rate-limits, and the limits are neither published
reliably nor stable month to month. A single-provider client therefore fails for
reasons that have nothing to do with the enquiry. The chain tries providers in
order and moves on when one is cold:

    1. Groq        (GROQ_API_KEY)        OpenAI-compatible, fast
    2. Gemini      (GEMINI_API_KEY)      AI Studio, key in a HEADER
    3. OpenRouter  (OPENROUTER_API_KEY)  nemotron, last resort

A provider with no key is skipped silently -- the chain is whatever keys are
actually configured, so a machine holding one key works exactly as well as a
machine holding three. Only an empty chain (no keys at all) is a ConfigError.

WHAT CAUSES A FALLTHROUGH, AND WHAT DOES NOT
--------------------------------------------
Fall through to the next provider on anything that says "this provider, right
now" rather than "this request": rate limits (429), server errors (5xx),
timeouts, connection failures, an empty completion, or a refusal. Credential
rejection (401/403) also falls through rather than killing the run -- a stale
key on provider 1 must not deny the run a working provider 2. Every skipped and
failed provider is recorded, so a silent fallthrough is never invisible.

A reply that arrives but is unusable -- not JSON, or JSON failing the proposal
invariants -- is NOT a fallthrough. A model answered; the answer was bad. That
is invalid_output, it is terminal, and it surfaces as llm_invalid_output. Asking
three providers in turn to produce prose that passes validation is how a
plausible-but-wrong proposal eventually slips through.

Fails loudly by design. There is no fallback text anywhere in this module: a
caller that cannot get a usable model response gets an LLMError, never a
degraded draft. A proposal is client-facing; a visible gap beats a plausible
template going out under someone's name.

Model ids and endpoints below were confirmed against each provider's own
documentation on 2026-09-13, not recalled. Each is overridable by environment
variable so a retired model id can be corrected without a code change.

DATA HANDLING -- read this before pointing the chain at real client work.
Enquiry text is client material: names, business details, how they run their
operation. All three providers here are free tiers, and free tiers are broadly
where providers reserve the right to log prompts and train on them. Treat every
proposal run as disclosing the enquiry to the provider that serves it. If that
is not acceptable for a given client, use a paid tier with a data-processing
agreement rather than this chain.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import approval as _approval  # noqa: E402

ConfigError = _approval.ConfigError

# Refuse rather than truncate above this. A huge paste must not run up a bill
# or silently lose the part of the enquiry that mattered.
MAX_ENQUIRY_CHARS = 8000

DEFAULT_TIMEOUT = 60

# urllib's default User-Agent is "Python-urllib/3.x", which Groq's Cloudflare
# edge rejects outright with 403 "error code: 1010" -- a browser-signature ban,
# before the key is ever examined. Verified 2026-09-13: identical request, same
# key, 403 with the default UA and 200 with this one. Without it the chain would
# skip Groq on every single run and blame the provider for being cold.
USER_AGENT = "juma-freelance-ai/1.0"

# Groq serves constrained decoding (response_format json_schema, strict:true)
# only on these model ids; every other Groq model accepts json_object mode but
# not a schema. Confirmed against Groq's structured-outputs documentation on
# 2026-09-13. Sending json_schema to a model that lacks it is a 400, which the
# chain would report as a cold provider -- so capability is checked, not assumed.
GROQ_STRICT_SCHEMA_MODELS = {
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
}

# One attempt per provider. The chain is the retry: when a provider is rate
# limited, backing off and asking it again is strictly worse than asking the
# next provider immediately.


class LLMError(RuntimeError):
    """Model call failed. kind is one of: api_error | invalid_output."""

    def __init__(self, message, kind="api_error", detail=None, attempts=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail
        # Per-provider outcomes, so a failed run says which providers were tried
        # and why each declined, not merely that "the model" was unavailable.
        self.attempts = attempts or []


# Prefix on an attempt reason meaning "the model replied and the reply broke the
# schema", as opposed to "this provider could not serve us".
SCHEMA_REJECTION_MARKER = "[schema-rejected] "

_SCHEMA_REJECTION_TEXT = (
    "does not match the expected schema",
    "failed_generation",
    "response_format",
)


def _is_schema_rejection(code, detail) -> bool:
    if code != 400:
        return False
    lowered = (detail or "").lower()
    return any(t in lowered for t in _SCHEMA_REJECTION_TEXT)


class _Fallthrough(Exception):
    """This provider cannot serve the request now; try the next one."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _env(name):
    """Environment only, never config/juma.json -- that file is committed."""
    value = _approval._config_value(name)
    return value or None


def _post(url, headers, payload, timeout):
    """POST JSON, return the parsed body. Raises _Fallthrough on provider fault."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        # 429 and 5xx are the provider; 401/403 is our key. Both are reasons to
        # use a different provider, not reasons to abandon the run.
        #
        # A constrained-decoding rejection is different in kind. Groq answers a
        # schema violation with 400 "Generated JSON does not match the expected
        # schema": the provider was reachable and the model replied -- the reply
        # was unusable. Still worth trying the next provider, since a different
        # model may conform, but the attempt is tagged so that an exhausted
        # chain reports invalid_output rather than claiming nothing was
        # reachable.
        marker = SCHEMA_REJECTION_MARKER if _is_schema_rejection(e.code, detail) else ""
        raise _Fallthrough(f"{marker}HTTP {e.code}: {detail}")
    except Exception as e:
        raise _Fallthrough(f"{type(e).__name__}: {e}")

    try:
        return json.loads(body)
    except Exception as e:
        raise _Fallthrough(f"provider returned non-JSON body: {e}")


def _openai_compatible(url, key, model, system, user, max_tokens, temperature,
                       timeout, extra_headers=None, schema=None,
                       strict_schema=False):
    """Groq and OpenRouter share OpenAI's request and response shape."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if schema and strict_schema:
        # Constrained decoding: the sampler cannot emit a token that breaks the
        # schema, so prose, preamble and fenced blocks become impossible rather
        # than merely discouraged.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "proposal", "strict": True, "schema": schema},
        }
    elif schema:
        # json_object is accepted by every OpenAI-compatible model here. It
        # guarantees parseable JSON, not a particular shape -- proposal._validate
        # is what enforces shape, identically for every provider.
        payload["response_format"] = {"type": "json_object"}

    parsed = _post(
        url,
        {"Authorization": f"Bearer {key}", **(extra_headers or {})},
        payload,
        timeout,
    )

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _Fallthrough(f"no choices returned: {str(parsed.get('error') or parsed)[:300]}")
    text = ((choices[0] or {}).get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise _Fallthrough("empty completion")
    return {"text": text, "model": parsed.get("model") or model,
            "usage": parsed.get("usage"), "raw": parsed}


def _call_groq(key, model, system, user, max_tokens, temperature, timeout,
               schema=None, strict_schema=True):
    return _openai_compatible(
        "https://api.groq.com/openai/v1/chat/completions",
        key, model, system, user, max_tokens, temperature, timeout,
        schema=schema,
        strict_schema=strict_schema and model in GROQ_STRICT_SCHEMA_MODELS)


def _call_openrouter(key, model, system, user, max_tokens, temperature, timeout,
                     schema=None, strict_schema=True):
    # json_object only. Schema support varies per upstream model on OpenRouter
    # and is not documented per-model for the free nemotron endpoint; sending an
    # unsupported response_format would 400 and read as an outage.
    return _openai_compatible(
        "https://openrouter.ai/api/v1/chat/completions",
        key, model, system, user, max_tokens, temperature, timeout,
        extra_headers={
            "HTTP-Referer": "https://github.com/juma-freelance-ai",
            "X-Title": "juma-freelance-ai",
        },
        schema=schema, strict_schema=False)


def _call_gemini(key, model, system, user, max_tokens, temperature, timeout,
                 schema=None, strict_schema=True):
    """AI Studio generateContent.

    The key goes in the x-goog-api-key HEADER, which is what Google's own REST
    example uses. automation/generate-proposal.ps1:103 puts it in the URL query
    string instead (audit S-1) -- URLs surface in exception messages, proxy logs
    and PowerShell transcripts, so that leaks the key into places nobody is
    treating as a credential store. Do not copy that pattern.
    """
    parsed = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},
        {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                # Gemini can enforce JSON at the decoder. This does not relax
                # any downstream check -- proposal._validate runs identically
                # for every provider -- it just wastes fewer calls on prose.
                #
                # responseMimeType is documented for generateContent and is used.
                # A responseSchema is deliberately NOT sent: Google documents
                # schema-constrained output for the newer Interactions API, not
                # for generateContent, and an unsupported generationConfig field
                # would 400 -- which this chain would report as a cold provider
                # rather than as our bug. Add it once it is confirmed here.
                "responseMimeType": "application/json",
            },
        },
        timeout,
    )

    candidates = parsed.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = parsed.get("promptFeedback") or parsed.get("error") or parsed
        raise _Fallthrough(f"no candidates returned: {str(feedback)[:300]}")

    candidate = candidates[0] or {}
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        # MAX_TOKENS, SAFETY or RECITATION land here: a candidate with no usable
        # text. Another provider may well answer, so move on rather than fail.
        raise _Fallthrough(f"no text in candidate (finishReason="
                           f"{candidate.get('finishReason')!r})")

    usage = parsed.get("usageMetadata") or {}
    return {
        "text": text,
        "model": parsed.get("modelVersion") or model,
        # Normalised to the OpenAI-shaped keys the evidence block already uses.
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        } if usage else None,
        "raw": parsed,
    }


# Order is the fallback order. Each model id was taken from the provider's own
# documentation on 2026-09-13; override via the named environment variable if a
# provider retires one.
PROVIDERS = [
    {
        "name": "groq",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        # Production on Groq's model list (131k context, 65k max completion),
        # and one of the three ids that support strict constrained decoding.
        # That capability is the reason it is preferred to
        # llama-3.3-70b-versatile here: this boundary needs a bare JSON object,
        # and a schema-constrained decoder cannot emit anything else.
        "default_model": "openai/gpt-oss-120b",
        "call": _call_groq,
    },
    {
        "name": "gemini",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        # Generally available, and the flash tier Google positions for
        # low-latency high-volume work.
        "default_model": "gemini-2.5-flash",
        "call": _call_gemini,
    },
    {
        "name": "openrouter",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "nvidia/nemotron-3.5-lightning:free",
        "call": _call_openrouter,
    },
]


def configured_providers():
    """Providers holding a key, in chain order. Empty means nothing is set up."""
    return [p for p in PROVIDERS if _env(p["key_env"])]


def complete(system: str, user: str, *, model: str = None, max_tokens: int = 2000,
             temperature: float = 0.3, timeout: int = DEFAULT_TIMEOUT,
             schema: dict = None, strict_schema: bool = True) -> dict:
    """First usable completion from the provider chain.

    Returns {"text", "model", "provider", "usage", "raw", "attempts"}. Raises
    ConfigError when no provider has a key, and LLMError(api_error) when every
    configured provider declined.

    ``model`` overrides the model id for whichever provider serves the request;
    it does not select a provider.

    ``schema`` requests structured output where the provider supports it. It is
    an optimisation, never a substitute for validation: a provider that ignores
    it still has its reply checked by exactly the same rules.

    ``strict_schema=False`` asks for JSON without constrained decoding. Groq's
    strict mode requires additionalProperties:false and every property listed as
    required; a schema that relaxes either is rejected outright with HTTP 400
    before the model runs. For a reply whose shape is genuinely variable, a
    guaranteed-JSON object plus validation in code beats a decoder that refuses
    the schema.
    """
    available = configured_providers()
    if not available:
        raise ConfigError(
            "No model provider is configured; refusing to continue "
            "without a model. Set at least one of "
            + ", ".join(p["key_env"] for p in PROVIDERS)
            + " in the environment -- keys must never be stored in "
            "config/juma.json, which is committed."
        )

    attempts = []
    for provider in PROVIDERS:
        key = _env(provider["key_env"])
        if not key:
            # Not an error: the chain is whichever keys this machine holds.
            attempts.append({"provider": provider["name"], "outcome": "skipped",
                             "reason": f"{provider['key_env']} not set"})
            continue

        chosen = model or _env(provider["model_env"]) or provider["default_model"]
        try:
            result = provider["call"](key, chosen, system, user, max_tokens,
                                      temperature, timeout, schema=schema,
                                      strict_schema=strict_schema)
        except _Fallthrough as e:
            attempts.append({"provider": provider["name"], "model": chosen,
                             "outcome": "failed", "reason": e.reason})
            continue

        attempts.append({"provider": provider["name"], "model": result["model"],
                         "outcome": "ok"})
        result["provider"] = provider["name"]
        result["attempts"] = attempts
        return result

    tried = [a for a in attempts if a["outcome"] == "failed"]
    summary = "; ".join(f"{a['provider']}: {a['reason']}" for a in tried)
    # If any provider rejected the model's own output against the schema, the
    # honest report is that the reply was unusable -- not that nothing answered.
    if any(SCHEMA_REJECTION_MARKER in (a.get("reason") or "") for a in tried):
        raise LLMError(
            "model output was rejected against the requested schema "
            f"({len(tried)} provider(s) tried): {summary}",
            kind="invalid_output", detail=summary, attempts=attempts)
    raise LLMError(
        f"every configured provider declined ({len(tried)} tried): {summary}",
        kind="api_error", detail=summary, attempts=attempts)


def complete_json(system: str, user: str, **kwargs) -> dict:
    """complete() plus strict JSON parsing of the reply.

    Tolerates a fenced ```json block, since models commonly wrap JSON, but
    nothing else. A reply that is not an object is invalid_output.

    Note this does NOT fall through to another provider: a model that answered
    with unusable content is a content failure, not an availability one.
    """
    result = complete(system, user, **kwargs)
    text = result["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        data = json.loads(text)
    except Exception as e:
        raise LLMError(f"model reply was not valid JSON: {e}",
                       kind="invalid_output", detail=result["text"][:400],
                       attempts=result.get("attempts"))
    if not isinstance(data, dict):
        raise LLMError("model reply was valid JSON but not an object",
                       kind="invalid_output", detail=result["text"][:400],
                       attempts=result.get("attempts"))
    result["data"] = data
    return result
