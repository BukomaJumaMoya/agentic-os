#!/usr/bin/env python3
"""
Research Agent — standalone bounded process.

Authority: READ only
Purpose: research and evidence gathering only
Forbidden: external business actions, modifications, messages
"""

import sys
import json
import urllib.request
import urllib.parse
import urllib.error
import re
from html.parser import HTMLParser

AGENT_NAME = "research"
VERSION = "1.0.0"


class SimpleLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_href = None
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_href = dict(attrs).get("href")
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self.current_href:
            self.links.append({"href": self.current_href, "title": self.title.strip()})
            self.current_href = None
            self.title = ""
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


# A challenge page is served with a 200/202 and looks like an ordinary page, so
# it has to be recognised by content as well as by status.
_CHALLENGE_RE = re.compile(r"anomaly|challenge|captcha|unusual traffic", re.IGNORECASE)


def search_web(query, max_results=5):
    """Return (results, meta).

    meta always records what actually happened -- the URL, the HTTP status and
    a classified outcome -- so that a provider block, a transport failure and a
    genuinely empty result set can never be reported as the same thing.

    outcome is one of: complete | no_results | blocked | search_failed
    """
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    meta = {"url": url, "http_status": None, "outcome": None,
            "detail": None, "links_seen": 0}
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{AGENT_NAME}/{VERSION}"
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            meta["http_status"] = getattr(resp, "status", None)
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        meta["http_status"] = e.code
        meta["outcome"] = "blocked" if e.code in (202, 403, 429) else "search_failed"
        meta["detail"] = f"HTTP {e.code}: {e.reason}"
        return [], meta
    except Exception as e:
        meta["outcome"] = "search_failed"
        meta["detail"] = f"{type(e).__name__}: {e}"
        return [], meta

    # 202 Accepted from this endpoint is an anti-bot challenge, not a result
    # set. Reporting it as "no results" is a lie the caller cannot see through.
    if meta["http_status"] == 202 or _CHALLENGE_RE.search(html):
        meta["outcome"] = "blocked"
        meta["detail"] = (f"search provider returned an anti-bot challenge "
                          f"(HTTP {meta['http_status']}), not a result set")
        return [], meta

    parser = SimpleLinkParser()
    parser.feed(html)
    meta["links_seen"] = len(parser.links)

    results = []
    for link in parser.links[:max_results]:
        href = link.get("href", "")
        if href and href.startswith("http"):
            results.append({
                "url": href,
                "title": link.get("title", "") or href,
                "snippet": ""
            })

    meta["outcome"] = "complete" if results else "no_results"
    if not results:
        meta["detail"] = (f"provider returned HTTP {meta['http_status']} with "
                          f"{meta['links_seen']} links, none usable as results")
    return results, meta


def fetch_page(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{AGENT_NAME}/{VERSION}"
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:8000]


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
    fetch_content = bool(data.get("fetch_content", False))

    findings = []
    unknowns = []
    assumptions = []
    facts = []

    try:
        search_results, search_meta = search_web(query, max_sources)
    except Exception as e:
        # search_web is meant to classify its own failures; this is a bug guard.
        search_results = []
        search_meta = {"url": None, "http_status": None, "outcome": "search_failed",
                       "detail": f"unhandled {type(e).__name__}: {e}", "links_seen": 0}

    outcome = search_meta.get("outcome")
    if outcome == "blocked":
        unknowns.append(f"Web search blocked: {search_meta.get('detail')}")
    elif outcome == "search_failed":
        unknowns.append(f"Web search failed: {search_meta.get('detail')}")
    elif outcome == "no_results":
        unknowns.append("No search results returned")

    for r in search_results:
        finding = {
            "source": r["url"],
            "title": r["title"],
            "content": "",
            "confidence": "low"
        }
        if fetch_content:
            try:
                content = fetch_page(r["url"])
                finding["content"] = content
                finding["confidence"] = "medium"
            except Exception as e:
                unknowns.append(f"Could not fetch {r['url']}: {e}")
        findings.append(finding)

    output = {
        "agent": AGENT_NAME,
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
