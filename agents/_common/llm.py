#!/usr/bin/env python3
"""Model access for agents: Groq first, OpenRouter second.

WHY THE CHAIN CAME BACK
-----------------------
This started as an OpenRouter-only client, on the reasoning that each agent
should hold exactly one provider credential. That reasoning lost to evidence:
OpenRouter's free tier allows 50 model requests per DAY across all free models,
account-wide. A day's testing exhausts it, and no amount of listing fallback
*models* helps, because the cap is on the account, not the model. An agent that
cannot answer for eleven hours is not contained, it is broken.

So the chain is back, with two limbs instead of the original three:

    1. Groq        (GROQ_API_KEY)        primary -- fast, generous free tier
    2. OpenRouter  (OPENROUTER_API_KEY)  fallback

Gemini stays gone. It was dropped for a reason that still holds: a third
credential in every agent's .env buys a fallback that the first two already
cover between them.

A provider with no key is skipped silently, so a machine holding one key works
exactly as well as a machine holding two. Only an empty chain is an error.

THE USER-AGENT LINE IS LöAD-BEARING
-----------------------------------
urllib's default User-Agent is "Python-urllib/3.x", and Groq's Cloudflare edge
rejects it outright with 403 "error code: 1010" -- a browser-signature ban,
refused before the key is ever examined. This was hit and diagnosed once
already: identical request, same key, 403 with the default UA and 200 with an
explicit one. Without the header below, the chain would skip a perfectly
healthy Groq on every single call and report it as a cold provider.

WHAT SURVIVED, AND WHY EACH PART EARNED ITS PLACE
-------------------------------------------------
The fallthrough classification, which is the genuinely hard-won part:

  - 429, 5xx, timeout, connection failure, empty completion -> this model is
    cold right now. Try the next model in the agent's list.
  - 401/403 -> our key is bad. Still a fallthrough rather than a fatal error,
    because on a multi-model list a stale key is the same shape of problem,
    but it is recorded distinctly so a misconfiguration is not read as an
    outage.
  - A reply that arrives and is unusable (not JSON, fails validation) is NOT a
    fallthrough. A model answered; the answer was bad. Retrying that against
    three more models is how a plausible-but-wrong answer eventually gets
    through. It is terminal, and it says so.

  - The explicit User-Agent. urllib's default is "Python-urllib/3.x", which
    some provider edges reject outright with a 403 before the key is examined.
    Without this line the chain would skip a healthy provider on every call and
    blame the provider.

Every attempt is recorded, so a silent fallthrough is never invisible.

DATA HANDLING
-------------
Every model configured here is a free tier, and free tiers are broadly where
providers reserve the right to log prompts and train on them. Any client
material an agent sends -- an enquiry, a task description, a source file -- is
disclosed to OpenRouter and its upstream. If that is not acceptable for a given
client, point that agent's model at a paid tier with a data-processing
agreement; the model id is a single .env variable per agent for exactly this
reason.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import AgentError

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# See the module docstring. This is not cosmetic: without it Groq's edge
# returns 403 before looking at the key.
USER_AGENT = "juma-freelance-ai-agents/1.0"

DEFAULT_TIMEOUT = 90


class Provider:
    """One provider limb: where to send, which key, which models."""

    def __init__(self, name: str, endpoint: str, api_key: str,
                 models: list[str], extra_headers: dict | None = None):
        self.name = name
        self.endpoint = endpoint
        self.api_key = api_key
        self.models = models
        self.extra_headers = extra_headers or {}

    def headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            # Load-bearing; see the module docstring.
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }


class _Fallthrough(Exception):
    """This model cannot serve the request now; try the next one."""

    def __init__(self, reason: str, kind: str = "cold"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class LLM:
    """One agent's model access. Constructed once at startup.

    Tries each provider in order, and each of that provider's models in order.
    A provider with no key is skipped before any request is made.
    """

    def __init__(self, *, agent: str, groq_key: str = "", groq_models: list[str] | None = None,
                 openrouter_key: str = "", openrouter_models: list[str] | None = None,
                 timeout: int = DEFAULT_TIMEOUT, audit=None):
        self.agent = agent
        self.timeout = timeout
        self.audit = audit
        self.providers: list[Provider] = []

        if (groq_key or "").strip() and groq_models:
            self.providers.append(Provider(
                "groq", GROQ_ENDPOINT, groq_key.strip(), list(groq_models)))
        if (openrouter_key or "").strip() and openrouter_models:
            self.providers.append(Provider(
                "openrouter", OPENROUTER_ENDPOINT, openrouter_key.strip(),
                list(openrouter_models),
                extra_headers={
                    "HTTP-Referer": "https://github.com/juma-freelance-ai",
                    "X-Title": f"juma-freelance-ai-{agent}",
                }))

        if not self.providers:
            raise AgentError(
                "missing_key",
                "no model provider is configured for this agent. Set "
                "GROQ_API_KEY (with a GROQ model) or OPENROUTER_API_KEY (with "
                "an OpenRouter model) in this agent's .env.",
            )

    @property
    def models(self) -> list[str]:
        """Every (provider, model) pair as flat strings, for logging."""
        return [f"{p.name}:{m}" for p in self.providers for m in p.models]

    def complete(self, system: str, user: str, *, max_tokens: int = 2000,
                 temperature: float = 0.2, json_mode: bool = False,
                 tools: list[dict] | None = None,
                 messages: list[dict] | None = None) -> dict[str, Any]:
        """Run the model list until one answers. Returns the parsed response.

        `messages`, when given, replaces the system/user pair -- used by the
        multi-turn tool loop in the PM agent. The system message is still
        prepended, because an agent's rules are not something a loop gets to
        drop on turn four.
        """
        attempts: list[dict[str, str]] = []

        if messages is None:
            convo = [{"role": "system", "content": system},
                     {"role": "user", "content": user}]
        else:
            convo = [{"role": "system", "content": system}] + list(messages)

        for provider in self.providers:
            for model in provider.models:
                try:
                    result = self._call(provider, model, convo, max_tokens,
                                        temperature, json_mode, tools)
                except _Fallthrough as exc:
                    attempts.append({"provider": provider.name, "model": model,
                                     "outcome": exc.kind, "reason": exc.reason})
                    if self.audit:
                        self.audit.write("llm_fallthrough", provider=provider.name,
                                         model=model, kind=exc.kind,
                                         reason=exc.reason)
                    continue
                result["provider"] = provider.name
                result["attempts"] = attempts
                if self.audit:
                    self.audit.write("llm_ok", provider=provider.name,
                                     model=result.get("model"),
                                     usage=result.get("usage"),
                                     fell_through=len(attempts))
                return result

        detail = "; ".join(f"{a['provider']}/{a['model']}: {a['reason']}"
                           for a in attempts)
        raise AgentError(
            "model_unavailable",
            f"every configured provider declined. Tried {len(attempts)}: {detail}",
            retryable=True,
            extra={"attempts": attempts},
        )

    def _call(self, provider: Provider, model: str, messages: list[dict],
              max_tokens: int, temperature: float, json_mode: bool,
              tools: list[dict] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            # json_object, never json_schema. Groq serves constrained decoding
            # (response_format json_schema, strict:true) on only a handful of
            # model ids, and OpenRouter's per-upstream support is undocumented
            # for the endpoints used here. Sending an unsupported
            # response_format is a 400, which this chain would report as an
            # outage rather than as our own bug. json_object is accepted by
            # every OpenAI-compatible model in the chain; it guarantees
            # parseable JSON, not a particular shape, and the caller validates
            # shape identically for every provider.
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        body = self._post(provider, payload)

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            err = str(body.get("error") or body)[:300]
            raise _Fallthrough(f"no choices returned: {err}")

        message = (choices[0] or {}).get("message") or {}
        text = message.get("content")
        calls = message.get("tool_calls")

        # A tool-calling turn legitimately has no text. Only a turn with
        # neither text nor tool calls is empty.
        if not calls and not (isinstance(text, str) and text.strip()):
            finish = (choices[0] or {}).get("finish_reason")
            raise _Fallthrough(f"empty completion (finish_reason={finish!r})")

        return {
            "text": text if isinstance(text, str) else "",
            "tool_calls": calls or [],
            "model": body.get("model") or model,
            "usage": body.get("usage"),
            "finish_reason": (choices[0] or {}).get("finish_reason"),
        }

    def _post(self, provider: Provider, payload: dict) -> dict:
        request = urllib.request.Request(
            provider.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=provider.headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403):
                # Recorded distinctly: a rejected key is a thing to fix, not a
                # provider having a bad afternoon.
                raise _Fallthrough(f"HTTP {exc.code}: credential rejected. {detail}",
                                   kind="credential") from None
            if exc.code == 429:
                raise _Fallthrough(f"HTTP 429: rate limited. {detail}",
                                   kind="rate_limit") from None
            if exc.code >= 500:
                raise _Fallthrough(f"HTTP {exc.code}: provider error. {detail}",
                                   kind="provider_error") from None
            raise _Fallthrough(f"HTTP {exc.code}: {detail}", kind="rejected") from None
        except Exception as exc:  # timeouts, DNS, connection reset
            raise _Fallthrough(f"{type(exc).__name__}: {exc}", kind="transport") from None

        try:
            return json.loads(raw)
        except Exception as exc:
            raise _Fallthrough(f"provider returned non-JSON body: {exc}") from None


def parse_json_reply(text: str) -> Any:
    """Parse a model's JSON answer, tolerating a fenced block around it.

    Terminal on failure, never a fallthrough: the model answered and the answer
    was unusable. See the module docstring.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                pass
        raise AgentError(
            "invalid_output",
            "the model replied with something that is not JSON. This is not "
            "retried against another model: a bad answer is not an outage.",
        )
