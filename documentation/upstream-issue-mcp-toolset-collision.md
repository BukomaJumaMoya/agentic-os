# Draft upstream issue — NousResearch/hermes-agent

Not filed. Review and post at
<https://github.com/NousResearch/hermes-agent/issues/new>.

Searched before drafting: #30563, #31788, #103943, #19793, #3077 (the shadowing
side), #109791, #78102, #110916 (platform_toolsets/MCP naming), #73739
(`tool_search.enabled=false`), #25752 and #103028 (the `type/security` toolset
-bypass class). None reports the direction below — every existing report is
about MCP tools going *missing*, not about built-in tools being *granted*.

Suggested labels: `type/security`, `comp/tools`, `tool/mcp`, `area/config`.

---

**Title:** An MCP server named after a built-in toolset grants that entire
toolset, bypassing `platform_toolsets` (regression from #103943)

## Summary

If an `mcp_servers` entry is named the same as a built-in toolset, every tool in
that built-in toolset is added to the platform's surface — even when
`platform_toolsets.<platform>` does not list it and `hermes tools list` reports
it as disabled.

On a Telegram-only orchestrator restricted to six toolsets, this put `terminal`,
`execute_code`, `write_file`, `patch`, `search_files`, `delegate_task` and the
full browser toolset in front of the model. The only thing needed to trigger it
was an MCP server named `coding`.

This is the inverse of #30563, and it appears to be a regression from that
issue's fix (#103943): making the resolution a **union** so an MCP server is no
longer shadowed also means the built-in is no longer excluded.

## Impact

`platform_toolsets` is the documented way to restrict what a messaging platform
can do, and it is the boundary a public-facing bot depends on. Here it failed
open and silently: no warning at startup, no warning at registration, and every
config-reading diagnostic agreed the surface was restricted. The disagreement
was visible only on the wire.

Comparable to #25752 (`type/security`, P1), which was also a toolset restriction
that could be bypassed rather than a crash.

## Version

`hermes_cli.__version__` 0.21.3, commit `a782e2ee7`, Windows 11.

## Reproduction

1. Configure an MCP server whose name matches a built-in toolset — `coding`,
   `browser`, `kanban`, `homeassistant`, `terminal`, `file`, `web`, `project`:

   ```yaml
   mcp_servers:
     coding:                       # collides with the built-in "coding" toolset
       command: /path/to/python
       args: ["/path/to/agent/main.py"]
       tools:
         include: [start_code_task, get_status, get_result, list_changed_files]
         resources: false
         prompts: false
   ```

2. Restrict a platform to toolsets that do **not** include `coding`:

   ```yaml
   platform_toolsets:
     telegram: [clarify, memory, session_search, mcp-coding, mcp-pm, mcp-research]
   ```

3. `hermes tools list` and `hermes tools --summary` both report `terminal`,
   `file`, `browser` and `code_execution` as **disabled** for that platform.

4. `hermes prompt-size --platform telegram` reports **24 tools / 40,745 B**,
   listing `terminal`, `file`, `browser`, `code_execution`, `delegation`,
   `skills`, `web` and `vision`.

5. Send a real turn. The request body contains `terminal`, `execute_code`,
   `read_file`, `write_file`, `patch`, `search_files`, `browser_exec`,
   `browser_vault_*`, `delegate_task`, `web_search`, `web_extract` and
   `vision_analyze`.

Renaming the server `coding` → `coding_agent`, changing nothing else, drops the
surface to 3 built-in tools and 0 forbidden tools.

## Trace

```
hermes_cli/tools_config.py:686   _merge_mcp_servers()
    When the platform list names no real MCP server, `explicit_mcp_servers` is
    empty and the function returns `result | enabled_mcp_servers` — every
    enabled server NAME is merged into the enabled-TOOLSET list.
    (`mcp-coding` etc. are neither server names nor toolset names, so they
    resolve to nothing, which is what makes this branch fire.)

toolsets.py:177                  built-in composite "coding"
    "Coding-focused toolset: files, terminal, search, web docs, skills, todo,
    delegate, vision, browser" — 37 tools.

model_tools.py:324               _select_tool_names() -> resolve_toolset(name)
    The merged server name is resolved as a toolset.

toolsets.py:287-295              get_toolset()
    merged_tools = static tools | registry tools for that name, plus the
    `mcp-<name>` alias target. The comment at :289 is #103943's fix for #30563.
```

Measured with `resolve_toolset()` directly:

```
clarify         ->  1 tool
memory          ->  1 tool
session_search  ->  1 tool
research        ->  0 tools     (no built-in of that name — harmless)
pm              ->  0 tools     (no built-in of that name — harmless)
coding          -> 37 tools     <- browser_exec, delegate_task, execute_code,
                                   patch, read_file, search_files, terminal,
                                   vision_analyze, web_extract, web_search,
                                   write_file
```

## Why the existing warning does not cover it

#31788 added a collision warning at the MCP registration boundary. That warning
describes the *shadowing* risk ("the built-in wins, your MCP tools may not
appear"). After #103943 the behaviour is a union, so the user-facing risk is now
the opposite — and the more serious one — while the diagnostic still describes
the old failure. In this deployment no such warning appeared in `agent.log`
either.

## Suggested remediation

In rough order of preference:

1. **Never resolve an MCP server name as a built-in toolset.** Keep MCP tools on
   the `mcp-<name>` toolset only, and have `_merge_mcp_servers` merge
   `mcp-<name>` rather than the bare name. That fixes #30563's complaint
   (tools reach the model) without granting the built-in.
2. **Make the union opt-in**, e.g. `mcp_servers.<name>.merge_builtin_toolset:
   true`, defaulting to false. Silent privilege expansion should require an
   explicit statement.
3. **Refuse the collision at config load** with an actionable error naming the
   colliding built-in, the way an invalid toolset name is already reported.
4. **At minimum**, upgrade the #31788 warning to describe the current
   behaviour — "MCP server 'X' shares a name with built-in toolset 'X'; its N
   tools are now on every platform where the server is enabled" — and emit it
   at tool resolution, not only at registration.

Independently: `_merge_mcp_servers`'s default of merging every enabled MCP
server into every platform's surface is surprising on its own. #110916 asks for
per-platform MCP scoping and would also have prevented this.

## Workaround

Rename the server so it does not collide, and check any new server name against
`hermes tools list` before using it.
