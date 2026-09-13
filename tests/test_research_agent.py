#!/usr/bin/env python3
"""
Research agent — offline tests.

Every test replaces research._post, the module's single HTTP boundary. Nothing
here reaches Tavily or needs TAVILY_API_KEY, so the suite is safe for CI and
costs no search credits. Everything above the boundary runs for real: payload
construction, result filtering, content capping and outcome classification.
"""

import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# The agent is a standalone script, not a package module.
_spec = importlib.util.spec_from_file_location(
    "research_agent", BASE / "agents" / "research" / "main.py")
research = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(research)

passed = 0


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


class _Post:
    """Replace research._post for one test."""

    def __init__(self, status=200, body=None, raises=None):
        self.status = status
        self.body = body
        self.raises = raises
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = research._post

        def fake(url, headers, payload, timeout):
            self.calls.append({"url": url, "headers": headers,
                               "payload": payload, "timeout": timeout})
            if self.raises is not None:
                raise self.raises
            return self.status, self.body

        research._post = fake
        return self

    def __exit__(self, *exc):
        research._post = self._real
        return False


class _Key:
    """Set or clear TAVILY_API_KEY for one test."""

    def __init__(self, value="tvly-test-key-not-real"):
        self.value = value
        self._saved = None

    def __enter__(self):
        self._saved = os.environ.pop("TAVILY_API_KEY", None)
        if self.value:
            os.environ["TAVILY_API_KEY"] = self.value
        return self

    def __exit__(self, *exc):
        os.environ.pop("TAVILY_API_KEY", None)
        if self._saved is not None:
            os.environ["TAVILY_API_KEY"] = self._saved
        return False


def _body(results, **extra):
    d = {"query": "q", "results": results, "response_time": 0.5}
    d.update(extra)
    return d


def _result(url="https://example.com/a", content="extracted text", **extra):
    d = {"title": "A page", "url": url, "content": content, "score": 0.9}
    d.update(extra)
    return d


def test_complete_returns_results():
    with _Key(), _Post(200, _body([_result(), _result(url="https://example.com/b")])) as p:
        results, meta = research.search_web("opticians", 3)
    assert meta["outcome"] == "complete", meta
    assert meta["results_seen"] == 2
    assert meta["provider"] == "tavily"
    assert len(results) == 2
    assert results[0]["url"] == "https://example.com/a"
    assert p.calls[0]["url"] == research.API_URL
    ok("complete_returns_results")


def test_empty_results_is_no_results_not_blocked():
    with _Key(), _Post(200, _body([])):
        results, meta = research.search_web("q", 3)
    assert results == []
    assert meta["outcome"] == "no_results", meta
    ok("empty_results_is_no_results_not_blocked")


def test_rate_limit_is_blocked():
    err = urllib.error.HTTPError(research.API_URL, 429, "Too Many Requests", {}, None)
    with _Key(), _Post(raises=err):
        results, meta = research.search_web("q", 3)
    assert meta["outcome"] == "blocked", meta
    assert meta["http_status"] == 429
    assert "quota or rate limit" in meta["detail"]
    ok("rate_limit_is_blocked")


def test_forbidden_is_blocked():
    err = urllib.error.HTTPError(research.API_URL, 403, "Forbidden", {}, None)
    with _Key(), _Post(raises=err):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "blocked", meta
    ok("forbidden_is_blocked")


def test_rejected_credential_is_search_failed_not_blocked():
    """A bad key is a misconfiguration to fix, not a provider throttling us."""
    err = urllib.error.HTTPError(research.API_URL, 401, "Unauthorized", {}, None)
    with _Key(), _Post(raises=err):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "search_failed", meta
    assert "credential rejected" in meta["detail"]
    ok("rejected_credential_is_search_failed_not_blocked")


def test_server_error_is_search_failed():
    err = urllib.error.HTTPError(research.API_URL, 500, "Server Error", {}, None)
    with _Key(), _Post(raises=err):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "search_failed", meta
    ok("server_error_is_search_failed")


def test_transport_failure_is_search_failed():
    with _Key(), _Post(raises=TimeoutError("timed out")):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "search_failed", meta
    assert "TimeoutError" in meta["detail"]
    ok("transport_failure_is_search_failed")


def test_missing_key_is_search_failed_and_makes_no_call():
    with _Key(value=None), _Post(200, _body([_result()])) as p:
        results, meta = research.search_web("q", 3)
    assert results == []
    assert meta["outcome"] == "search_failed", meta
    assert "TAVILY_API_KEY" in meta["detail"]
    # No key means no request was attempted at all.
    assert p.calls == []
    ok("missing_key_is_search_failed_and_makes_no_call")


def test_non_json_body_is_search_failed():
    with _Key(), _Post(200, None):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "search_failed", meta
    ok("non_json_body_is_search_failed")


def test_missing_results_array_is_search_failed():
    with _Key(), _Post(200, {"query": "q"}):
        _, meta = research.search_web("q", 3)
    assert meta["outcome"] == "search_failed", meta
    ok("missing_results_array_is_search_failed")


def test_request_carries_bearer_auth_and_user_agent():
    with _Key("tvly-secret"), _Post(200, _body([_result()])) as p:
        research.search_web("q", 3)
    call = p.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tvly-secret"
    # The key must never travel in the URL.
    assert "tvly-secret" not in call["url"]
    # _post sets the User-Agent itself; assert the module defines a real one.
    assert research.USER_AGENT.startswith("juma-freelance-ai-research/")
    ok("request_carries_bearer_auth_and_user_agent")


def test_generated_answer_is_never_requested():
    """A model-written summary is not evidence and must not be fetched."""
    with _Key(), _Post(200, _body([_result()])) as p:
        research.search_web("q", 3)
    assert p.calls[0]["payload"]["include_answer"] is False
    ok("generated_answer_is_never_requested")


def test_fetch_content_selects_raw_extract():
    with _Key(), _Post(200, _body([_result(raw_content="the full page text")])) as p:
        results, _ = research.search_web("q", 3, include_raw=True)
    assert p.calls[0]["payload"]["include_raw_content"] == "text"
    assert results[0]["content"] == "the full page text"
    # The short extract is still kept separately.
    assert results[0]["snippet"] == "extracted text"
    ok("fetch_content_selects_raw_extract")


def test_raw_content_falls_back_to_content_when_absent():
    with _Key(), _Post(200, _body([_result()])):
        results, _ = research.search_web("q", 3, include_raw=True)
    assert results[0]["content"] == "extracted text"
    ok("raw_content_falls_back_to_content_when_absent")


def test_content_is_capped():
    huge = "x" * 50000
    with _Key(), _Post(200, _body([_result(raw_content=huge)])):
        results, _ = research.search_web("q", 3, include_raw=True)
    assert len(results[0]["content"]) == research.MAX_CONTENT_CHARS
    ok("content_is_capped")


def test_max_results_is_clamped():
    with _Key(), _Post(200, _body([])) as p:
        research.search_web("q", 999)
    assert p.calls[0]["payload"]["max_results"] == research.MAX_RESULTS_CEILING
    with _Key(), _Post(200, _body([])) as p:
        research.search_web("q", 0)
    assert p.calls[0]["payload"]["max_results"] == 1
    ok("max_results_is_clamped")


def test_malformed_results_are_filtered_out():
    bad = [
        "not a dict",
        {"url": "javascript:alert(1)", "title": "x"},
        {"url": "ftp://example.com/f", "title": "x"},
        {"title": "no url at all"},
        _result(url="https://example.com/good"),
    ]
    with _Key(), _Post(200, _body(bad)):
        results, meta = research.search_web("q", 10)
    assert meta["results_seen"] == 5
    assert [r["url"] for r in results] == ["https://example.com/good"]
    ok("malformed_results_are_filtered_out")


def test_no_scraping_helpers_remain():
    """fetch_page and SimpleLinkParser were deleted, not left dormant."""
    for gone in ("fetch_page", "SimpleLinkParser", "_CHALLENGE_RE"):
        assert not hasattr(research, gone), f"{gone} is still present"
    source = (BASE / "agents" / "research" / "main.py").read_text(encoding="utf-8")
    # The docstring explains the removal; no executable reference may survive.
    assert "HTMLParser" not in source
    assert "html.duckduckgo.com/html" not in source.split('"""')[2]
    ok("no_scraping_helpers_remain")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL RESEARCH AGENT TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
