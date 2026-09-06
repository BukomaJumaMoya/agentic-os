# HERMES DELIVERABLE 1 — Phase 3: Research Agent Definition

**Date**: 2026-09-04  
**Status**: Phase 3 COMPLETE — research capability defined

---

## 1. ROLE

The Research Agent is a **standalone separate process** that performs bounded research tasks for Hermes.

Research answers questions like:
- "Find competitors for X"
- "Research Y technology / framework / approach"
- "What's the latest on Z topic?"
- "Summarize this article / documentation"
- "Compare X vs Y approaches"
- "What are the best practices for X?"

---

## 2. TOOLS

| Tool | What it does | When to use |
|---|---|---|
| `web_search(query, limit)` | Searches the web, returns top results with URLs + snippets | Initial research; find sources |
| `web_extract(urls, char_limit)` | Extracts full content from URLs as markdown/text | Read the sources found by web_search |
| `browser_exec(code)` | Runs browser automation (navigate, click, fill, extract DOM) | Interactive web tasks; form filling; JS-heavy pages; verifying pages work |

**Research flow:**
1. `web_search` → find sources
2. `web_extract` the most relevant sources → get content
3. Synthesize findings into a coherent answer
4. Optionally `browser_exec` to verify or interact with a page

---

## 3. OUTPUT FORMAT

Research output should include:
- What was found (facts, data, comparisons)
- Sources used (URLs)
- Confidence level (what's well-sourced vs. speculative)
- What's unknown / needs more research

---

## 4. LIMITS

- Research is bounded by Hermes's tool capabilities — it can search, extract, and browse, but can't access paywalled content, private databases, or data that requires authentication Hermes doesn't have.
- Research uses Hermes's primary model (Solar Pro4 via Nous Portal) for synthesis. If that's unavailable, fall back to OpenClaw.
- web_extract has a char_limit (default 15000); larger pages are truncated with full text saved to disk — research may need to read the saved file for full content.

---

## 5. WHEN NOT TO USE RESEARCH

- Code generation → use Gemini (Phase 5)
- Task/project management → use ClickUp (Phase 4)
- General Q&A that Hermes can answer from its own knowledge → Hermes direct (no research needed)
- Anything requiring human judgment → ask user (Phase 17)

---

*Phase 3 complete. Research is a Hermes capability using web_search + web_extract + browser_exec + Solar Pro4 synthesis.*
