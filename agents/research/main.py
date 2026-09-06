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


def search_web(query, max_results=5):
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{AGENT_NAME}/{VERSION}"
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    parser = SimpleLinkParser()
    parser.feed(html)

    results = []
    for link in parser.links[:max_results]:
        href = link.get("href", "")
        if href and href.startswith("http"):
            results.append({
                "url": href,
                "title": link.get("title", "") or href,
                "snippet": ""
            })
    return results


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
        search_results = search_web(query, max_sources)
    except Exception as e:
        unknowns.append(f"Web search failed: {e}")
        search_results = []

    if not search_results:
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
        "status": "complete" if findings else "no_results"
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
