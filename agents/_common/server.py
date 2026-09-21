#!/usr/bin/env python3
"""Agent bootstrap: one call that wires an agent up the same way every time.

An agent module's whole startup becomes:

    boot = bootstrap(
        agent="research",
        version="3.0.0",
        required=["OPENROUTER_API_KEY", "TAVILY_API_KEY"],
        default_model="nex-agi/nex-n2.5-pro:free",
        instructions=INSTRUCTIONS,
    )

and it now holds a server, an audit log, a job registry, an LLM client and a
guarded-tool decorator, with the inherited environment already scrubbed.

ORDER MATTERS HERE
------------------
Scrub, then register secrets with the redactor, then open the audit log. Doing
it the other way round means the first few log lines are written before the
redactor knows what this agent's key looks like -- and the first few lines are
the startup lines, which are exactly the ones that mention configuration.

STDOUT IS THE PROTOCOL
----------------------
stdio transport means stdout carries JSON-RPC frames. Anything else printed
there corrupts the stream and Hermes sees a dead server with no explanation.
So this module points logging at stderr and says so loudly, because `print()`
in an agent is otherwise a very natural thing to write.

A NOTE ON THE SDK
-----------------
The official SDK renamed FastMCP to MCPServer in mcp 2.x. Same class, same
decorator API; the import is the only difference. It is aliased below so agent
code reads the way the SDK's own documentation reads.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import audit as _audit
from . import env as _env
from . import errors
from . import jobs as _jobs
from .llm import LLM

# The name the SDK used before 2.x, and the name the architecture document
# uses. Kept so agent code and docs agree.
FastMCP = MCPServer


@dataclass
class Boot:
    agent: str
    version: str
    server: MCPServer
    audit: _audit.Audit
    jobs: _jobs.JobRegistry
    config: dict
    llm: LLM | None

    def tool(self, *args, **kwargs) -> Callable:
        """Register an MCP tool that cannot leak a traceback or a secret."""
        def decorate(func: Callable) -> Callable:
            wrapped = errors.guarded(audit=self.audit)(func)
            return self.server.tool(*args, **kwargs)(wrapped)
        return decorate

    def run(self) -> None:
        self.audit.write("server_start", version=self.version,
                         tools=sorted(_tool_names(self.server)))
        self.server.run(transport="stdio")


def _tool_names(server: MCPServer) -> list[str]:
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) if manager else None
    if isinstance(tools, dict):
        return list(tools)
    return []


def bootstrap(*, agent: str, version: str, instructions: str,
              required: list[str] | None = None,
              optional: list[str] | None = None,
              default_model: str | None = None,
              fallback_models: list[str] | None = None,
              needs_llm: bool = True) -> Boot:
    # stdout is the JSON-RPC channel. Every log byte goes to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format=f"%(asctime)s {agent} %(levelname)s %(message)s",
    )

    optional = list(optional or [])
    # Per-agent model configuration. <AGENT>_MODEL names the Groq model (the
    # primary), <AGENT>_OPENROUTER_MODEL the fallback; FALLBACK_MODELS adds
    # extra Groq models tried before the chain moves to OpenRouter.
    model_var = f"{agent.upper()}_MODEL"
    openrouter_var = f"{agent.upper()}_OPENROUTER_MODEL"
    for name in (model_var, openrouter_var, "FALLBACK_MODELS",
                 "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        if name not in optional and name not in (required or []):
            optional.append(name)

    config = _env.load(agent, required=required, optional=optional)
    errors.register_secrets(_env.secret_values(config))

    log = _audit.Audit(agent)
    log.write("env_loaded",
              env_file=config["_env_file"],
              declared=sorted(k for k in config if not k.startswith("_")),
              scrubbed_inherited=config["_scrubbed"],
              ignored_undeclared=config["_undeclared"])

    def _csv(value: str) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    groq_models: list[str] = []
    configured = (config.get(model_var) or "").strip()
    if configured:
        groq_models.append(configured)
    elif default_model:
        groq_models.append(default_model)
    groq_models += _csv(config.get("FALLBACK_MODELS", ""))
    if not _csv(config.get("FALLBACK_MODELS", "")) and fallback_models:
        groq_models += fallback_models

    openrouter_models = _csv(config.get(openrouter_var, ""))

    llm = None
    if needs_llm:
        llm = LLM(
            agent=agent,
            groq_key=config.get("GROQ_API_KEY", ""),
            groq_models=groq_models,
            openrouter_key=config.get("OPENROUTER_API_KEY", ""),
            openrouter_models=openrouter_models,
            audit=log,
        )
        log.write("llm_configured", chain=llm.models)

    server = MCPServer(
        name=f"juma-{agent}",
        version=version,
        instructions=instructions,
    )

    return Boot(agent=agent, version=version, server=server, audit=log,
                jobs=_jobs.JobRegistry(audit=log), config=config, llm=llm)


def fatal(agent: str, exc: BaseException) -> None:
    """Die usefully.

    A failed bootstrap must not print to stdout: Hermes is reading JSON-RPC
    frames there and a stray line reads as a protocol error, which is a much
    worse diagnostic than "OPENROUTER_API_KEY is missing".
    """
    detail = errors.redact(f"{type(exc).__name__}: {exc}")
    print(f"[{agent}] cannot start: {detail}", file=sys.stderr, flush=True)
    sys.exit(1)


def as_any(value: Any) -> Any:
    return value
