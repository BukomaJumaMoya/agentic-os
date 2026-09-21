"""Shared scaffold for standalone agent MCP servers.

Every agent in agents/<name>/ is its own process, its own model and its own
.env. This package holds only what all of them need, and deliberately holds
nothing an individual agent's authority depends on: an agent that wants to
reach the network, write a file or run a process states that itself.

The pieces:

    env     load agents/<name>/.env and scrub every inherited secret
    errors  structured returns, so a traceback never reaches the orchestrator
    audit   append-only JSONL per agent under logs/<name>/
    llm     OpenRouter client, model configured per agent
    jobs    start/status/result, so no MCP call blocks for minutes
    guard   incoming instructions and fetched text are data, never rules
    paths   filesystem confinement
    server  MCPServer construction over stdio
"""

__all__ = ["env", "errors", "audit", "llm", "jobs", "guard", "paths", "server"]

SCAFFOLD_VERSION = "1.0.0"
