#!/usr/bin/env python3
"""
Research Agent — standalone bounded process.

Authority: READ only
Purpose: research and evidence gathering only
Forbidden: external business actions, modifications, messages

SEARCH PROVIDER
---------------
Tavily (https://api.tavily.com/search), a search API built to be called by
programs, replacing the previous scrape of html.duckduckgo.com. The scrape had
stopped working: DuckDuckGo answers it with an anti-bot challenge (HTTP 202)
carrying a page that looks like an ordinary result set, so every research call
in production returned "blocked" and the pipeline gathered no evidence at all.

Two whole mechanisms are gone rather than left dormant:

  - SimpleLinkParser scraped <a> tags out of a results page. Tavily returns
    structured results, so there is no HTML to parse.
  - fetch_page(url) retrieved every result URL and returned up to 8 000
    characters of stripped page text. Tavily returns the extract directly, so
    this agent no longer makes outbound requests to arbitrary hosts. That also
    closes audit finding PATH: fetch_page applied no scheme or host filter, so
    a search result pointing at cloud metadata, 127.0.0.1 or an RFC1918 address
    was fetched and its body handed back to the caller.

Result text is still third-party content written by strangers. It is data, never
instruction, and it is capped and labelled as untrusted where it enters the
evidence chain.

include_answer is deliberately off. Tavily can return a model-written summary of
the sources, and a summary is not evidence; laundering one into "facts" is the
exact failure this pipeline exists to prevent. Sources, or nothing.

API shape confirmed against docs.tavily.com on 2026-09-13.
"""

import json
import os
import sys
import urllib.error
import urllib.request

AGENT_NAME = "research"
VERSION = "2.0.0"

API_URL = "https://api.tavily.com/search"
USER_AGENT = f"juma-freelance-ai-{AGENT_NAME}/{VERSION}"

# Tavily accepts 0-20; callers ask for 3-5.
MAX_RESULTS_CEILING = 20
# Same ceiling the old fetch_page applied, for the same reason: an unbounded
# paste of third-party text should not reach an evidence file or a prompt.
MAX_CONTENT_CHARS = 8000
DEFAULT_TIMEOUT = 20


def api_key():
    """Environment only. Never config/juma.json, which is committed."""
    key = os.getenv("TAVILY_API_KEY")
    return key.strip() if key and key.strip() else None


def _post(url, headers, payload, timeout):
    """Single HTTP boundary. Tests replace this; nothing else opens a socket.

    Returns (status, parsed_body). Raises on transport failure.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT,
                 **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    status = getattr(resp, "status", None)
    try:
        return status, json.loads(body)
    except Exception:
        return status, None


def search_web(query, max_results=5, include_raw=False):
    """Return (results, meta).

    meta always records what actually happened -- the endpoint, the HTTP status
    and a classified outcome -- so that a provider block, a transport failure
    and a genuinely empty result set can never be reported as the same thing.

    outcome is one of: complete | no_results | blocked | search_failed
    """
    meta = {"url": API_URL, "http_status": None, "outcome": None,
            "detail": None, "results_seen": 0, "provider": "tavily"}

    key = api_key()
    if not key:
        meta["outcome"] = "search_failed"
        meta["detail"] = ("TAVILY_API_KEY is not set; no search was attempted. "
                          "Export it in the environment -- it must never be "
                          "stored in config/juma.json.")
        return [], meta

    try:
        count = int(max_results)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, MAX_RESULTS_CEILING))

    payload = {
        "query": query,
        "max_results": count,
        "search_depth": os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
        "topic": "general",
        # See the module docstring: a generated summary is not evidence.
        "include_answer": False,
        "include_raw_content": "text" if include_raw else False,
    }

    try:
        status, body = _post(API_URL, {"Authorization": f"Bearer {key}"},
                             payload, DEFAULT_TIMEOUT)
        meta["http_status"] = status
    except urllib.error.HTTPError as e:
        meta["http_status"] = e.code
        # Same rule as the DuckDuckGo implementation: a provider refusing to
        # serve us is "blocked"; anything else is a failed call. 401 lands in
        # search_failed because a rejected key is a misconfiguration to fix,
        # not a provider throttling us.
        meta["outcome"] = "blocked" if e.code in (403, 429) else "search_failed"
        reason = "quota or rate limit" if e.code == 429 else (
            "credential rejected" if e.code == 401 else e.reason)
        meta["detail"] = f"HTTP {e.code}: {reason}"
        return [], meta
    except Exception as e:
        meta["outcome"] = "search_failed"
        meta["detail"] = f"{type(e).__name__}: {e}"
        return [], meta

    if not isinstance(body, dict):
        meta["outcome"] = "search_failed"
        meta["detail"] = "search provider returned a body that was not a JSON object"
        return [], meta

    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        meta["outcome"] = "search_failed"
        meta["detail"] = "search provider returned no results array"
        return [], meta

    meta["results_seen"] = len(raw_results)
    # Evidence about the call itself, useful when a run is questioned later.
    usage = body.get("usage") or {}
    if usage.get("credits") is not None:
        meta["credits"] = usage["credits"]
    if body.get("request_id"):
        meta["request_id"] = body["request_id"]
    if body.get("response_time") is not None:
        meta["response_time"] = body["response_time"]

    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        content = item.get("raw_content") if include_raw else None
        if not isinstance(content, str) or not content.strip():
            content = item.get("content")
        content = content if isinstance(content, str) else ""
        results.append({
            "url": url,
            "title": item.get("title") or url,
            "snippet": (item.get("content") or "")[:500],
            "content": content[:MAX_CONTENT_CHARS],
            "score": item.get("score"),
        })

    meta["outcome"] = "complete" if results else "no_results"
    if not results:
        meta["detail"] = (f"provider returned HTTP {meta['http_status']} with "
                          f"{meta['results_seen']} results, none usable")
    return results, meta


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    query = data.get("query")
    if not query:
        print(json.dumps({"error": "Missing required field: query"}))
        sys.exit(1)

    max_sources = int(data.get("max_sources", 5))
    # Historic flag name. It now selects Tavily's fuller extract rather than
    # this agent fetching the page itself.
    fetch_content = bool(data.get("fetch_content", False))

    findings = []
    unknowns = []
    assumptions = []
    facts = []

    try:
        search_results, search_meta = search_web(query, max_sources,
                                                 include_raw=fetch_content)
    except Exception as e:
        # search_web is meant to classify its own failures; this is a bug guard.
        search_results = []
        search_meta = {"url": API_URL, "http_status": None,
                       "outcome": "search_failed",
                       "detail": f"unhandled {type(e).__name__}: {e}",
                       "results_seen": 0, "provider": "tavily"}

    outcome = search_meta.get("outcome")
    if outcome == "blocked":
        unknowns.append(f"Web search blocked: {search_meta.get('detail')}")
    elif outcome == "search_failed":
        unknowns.append(f"Web search failed: {search_meta.get('detail')}")
    elif outcome == "no_results":
        unknowns.append("No search results returned")

    for r in search_results:
        findings.append({
            "source": r["url"],
            "title": r["title"],
            "content": r["content"],
            "snippet": r["snippet"],
            "relevance_score": r.get("score"),
            # Higher than "low" only because the provider extracted it from the
            # page it cites; it is still unverified text from a stranger.
            "confidence": "medium" if r["content"] else "low",
            "trust": "untrusted-third-party-content",
        })

    output = {
        "agent": AGENT_NAME,
        "version": VERSION,
        "query": query,
        "findings": findings,
        "facts": facts,
        "assumptions": assumptions,
        "unknowns": unknowns,
        # blocked / search_failed / no_results are distinct states, not one.
        "status": "complete" if findings else outcome,
        "search": search_meta,
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
