# OpenClaw → Hermes Integration Investigation

**Date:** 2026-09-06  
**Updated:** 2026-09-07  
**Status:** Superseded by implementation.  
**Implementer:** Hermes Agent (executing on behalf of JUMA Moya)  
**Scope:** Installed OpenClaw at `C:\Users\HP\AppData\Roaming\npm\node_modules\openclaw\`, version `2026.9.1`

---

## 1. OpenClaw Version Inspected

- **Version:** `2026.9.1` (from `meta.lastTouchedVersion` in `C:\Users\HP\.openclaw\openclaw.json`)
- **Installation:** npm global at `C:\Users\HP\AppData\Roaming\npm\node_modules\openclaw\`
- **Node compatibility:** requires Node 22.22.3+, 24.15+, or 25.9+
- **Host Node:** `24.19.0`

## 2. Relevant Documentation Inspected

- `docs/plugins/tool-plugins.md`
- `docs/plugins/building-plugins.md`
- `docs/plugins/cli-backend-plugins.md`
- `docs/gateway/tools-invoke-http-api.md`
- `docs/channels/channel-routing.md`
- `docs/automation/hooks.md`
- `docs/concepts/session-tool.md`
- `docs/gateway/config-tools.md`
- `docs/tools/plugin.md`
- `docs/snippets/plugin-publish/minimal-openclaw.plugin.json`

## 3. Supported Integration Mechanisms Discovered

### A. Tool Plugins (supported)
- **Mechanism:** `defineToolPlugin` or `definePluginEntry` + `api.registerTool(...)`
- **Contract:** Requires plugin package with `package.json`, `openclaw.plugin.json`, `dist/`, manifest `contracts.tools`
- **Behavior:** Tool becomes available to OpenClaw's agent runtime. The agent/model decides when to call it based on the prompt.
- **Relevant docs:** `docs/plugins/tool-plugins.md`, `docs/plugins/building-plugins.md`

### B. CLI Backend Plugins (supported)
- **Mechanism:** `api.registerCliBackend(...)`
- **Contract:** Declares a local CLI as a model backend with command/args/JSONL parsing
- **Behavior:** Makes a CLI appear as a model provider prefix, e.g. `hermes/chat`
- **Limitation:** Designed for text-inference CLIs that speak OpenClaw's streaming JSONL dialect. Hermes does not.
- **Relevant docs:** `docs/plugins/cli-backend-plugins.md`

### C. Plugin Hooks (supported)
- **Mechanism:** Internal hooks (`HOOK.md`) or plugin hooks (`api.on(...)`)
- **Contract:** Event-key array, handler function
- **Behavior:** React to lifecycle/session/message events as side effects
- **Limitation:** Hooks are not a message-routing surface. `event.messages` is ignored as a reply for most events. No clean "intercept and replace reply" contract for inbound chat messages.
- **Relevant docs:** `docs/automation/hooks.md`, `docs/plugins/hooks.md`

### D. HTTP `/tools/invoke` Endpoint (supported)
- **Mechanism:** `POST http://127.0.0.1:18789/tools/invoke` with Gateway bearer token
- **Behavior:** Invoke a single tool by name with operator-level auth
- **Limitation:** Operator surface. Not a message-routing path. Requires auth token.
- **Relevant docs:** `docs/gateway/tools-invoke-http-api.md`

### E. Webhooks (supported)
- **Mechanism:** Outbound HTTP POST from OpenClaw on events
- **Behavior:** Push notification to external service
- **Limitation:** Inverts the direction. Hermes would need to be a webhook receiver, which it is not.
- **Relevant docs:** `docs/automation/cron-jobs.md` (webhooks section)

### F. Channel Plugins (supported, heavyweight)
- **Mechanism:** `defineChannelPluginEntry`
- **Behavior:** Custom messaging channel
- **Limitation:** Telegram is already a built-in channel. Writing a custom channel plugin to wrap Telegram just to route to Hermes is architectural overkill and competes with the existing telegram plugin.

### G. Session/Conversation Tools (supported, intra-OpenClaw)
- **Mechanism:** `sessions_send`, `conversations_send`, `conversations_turn`
- **Behavior:** Cross-session messaging within the same Gateway
- **Limitation:** Operates within OpenClaw's session model. Hermes is not an OpenClaw session.

### H. MCP Server Config (supported for tools)
- **Mechanism:** `mcp.servers` with command/args
- **Behavior:** Exposes MCP tools inside OpenClaw
- **Limitation:** Tool surface, not message router. Hermes does not speak MCP.

## 4. How Each Mechanism Could or Could Not Connect to Hermes

| Mechanism | Can connect to Hermes? | Why/Why not |
|-----------|------------------------|-------------|
| Tool Plugin | **Partially** | Can spawn `hermes chat -q` as a tool call. Agent decides when to invoke. Returns Hermes output as tool result. Does NOT give deterministic routing of all messages. |
| CLI Backend | **No** | Requires Hermes to speak OpenClaw's JSONL streaming protocol. Hermes is a chat CLI, not a streaming model backend. |
| Plugin Hook | **No** | No supported contract to intercept inbound Telegram DMs and replace the agent reply with Hermes output. `event.messages` is ignored for reply purposes on most events. |
| `/tools/invoke` | **No** | Operator auth surface. Not a message router. External callers could invoke a Hermes tool, but Telegram messages don't automatically flow through it. |
| Webhook | **No** | Outbound only. Hermes would need to be an HTTP service. |
| Channel Plugin | **Possible but wrong shape** | Would require reimplementing/replacing the telegram channel just to hook the dispatch. Violates architecture preservation. |
| Session Tools | **No** | Hermes is not an OpenClaw session. |
| MCP Server | **No** | Hermes does not implement MCP. |

## 5. Recommended Mechanism

**Tool Plugin** — specifically, a conformant OpenClaw tool plugin that registers an `invoke_hermes` tool.

### Why this is the supported path
- It uses OpenClaw's documented plugin SDK (`openclaw/plugin-sdk/tool-plugin` or `openclaw/plugin-sdk/plugin-entry`)
- It requires the standard plugin package structure: `package.json`, `openclaw.plugin.json`, `dist/`
- It is installed via `openclaw plugins install` and loaded by the Gateway at startup
- The tool can invoke `hermes chat -q` via child process and return the result
- OpenClaw's agent runtime can call it, and the result flows back through OpenClaw's reply path to Telegram

### Why other paths were rejected
- **CLI backend:** wrong abstraction; Hermes isn't a model provider
- **Hook:** no supported reply-replacement contract for inbound DMs
- **Channel plugin:** architectural overkill; would replace telegram, not route through it
- **Webhook:** wrong direction; outbound only
- **`/tools/invoke`:** operator surface, not message router

## 6. Required Components

1. **Plugin package directory** (e.g. `tools/openclaw-hermes-router-plugin/`):
   - `package.json` with `openclaw.extensions`, `peerDependencies`, `dependencies`
   - `openclaw.plugin.json` manifest with `id`, `contracts.tools`, `activation.onStartup`
   - `src/index.ts` using `definePluginEntry` or `defineToolPlugin`
   - `tsconfig.json` for ESM build
   - Tool implementation: spawn `hermes chat -q <message>`, capture stdout/stderr, return JSON

2. **Installation command:**
   - `openclaw plugins install --link ./tools/openclaw-hermes-router-plugin` (local dev)
   - Or `npm pack` + `openclaw plugins install npm-pack:...` (package proof)

3. **OpenClaw config:**
   - `plugins.entries.openclaw-hermes-router.enabled: true`
   - `plugins.entries.openclaw-hermes-router.config.host/port/script` (if needed)
   - No stale entry removal until plugin is verified loaded

4. **Gateway restart:**
   - `openclaw gateway restart` or scheduled task restart

## 7. Exact Message/Data Flow (Supported Path)

```
Juma Telegram message
    ↓
Telegram channel plugin receives message
    ↓
Channel routing binds to agent `main` session
    ↓
OpenClaw agent runtime processes turn
    ↓
Agent/model selects `invoke_hermes` tool
    ↓
Tool plugin spawns: hermes chat -q "<message>"
    ↓
Hermes processes via orchestrator/specialists
    ↓
Hermes returns response text
    ↓
Tool returns { content: [{type:"text", text: response}], details: {...} }
    ↓
OpenClaw agent turn completes with tool result
    ↓
OpenClaw routes reply back through Telegram channel
    ↓
Juma receives Hermes response in Telegram
```

### Important caveat
This is **agent-discretionary routing**, not deterministic routing. OpenClaw's model decides whether to call `invoke_hermes`. The architecture document implies deterministic routing, but no supported mechanism provides that without a custom channel plugin or unsupported hook hack.

## 8. Security Considerations

- **Child process execution:** The tool plugin spawns `hermes` as a child process. This is a local execution surface. OpenClaw's tool policy/sandbox applies.
- **Credential exposure:** `hermes chat -q` args appear in process listings briefly. The tool should avoid logging full prompts/args.
- **Gateway auth:** The `/tools/invoke` endpoint uses full operator auth. A tool plugin loaded in the agent runtime does not require bearer tokens for internal invocation.
- **Delivery boundary:** `toolContext.delivery?.send()` is channel-owned; plugins cannot retarget it.
- **Plugin trust:** Installing a plugin is running code. The plugin package must be reviewed before install.

## 9. Existing `tools/openclaw-hermes-router.js` Assessment

### Should it be retained, replaced, or discarded?

**Retained as reference, replaced as implementation.**

The existing file is a standalone Node HTTP server. It is NOT a conformant OpenClaw plugin package. It cannot be loaded by OpenClaw's plugin loader as-is.

However, its core logic — spawning `hermes chat -q` and returning the result — is correct and should be ported into the tool plugin's `execute()` function.

### Why the existing file is not viable
- Missing `openclaw.plugin.json`
- Missing `package.json` with OpenClaw extension metadata
- Missing built `dist/` output
- Not registered in OpenClaw's plugin index/registry
- OpenClaw explicitly logs: `plugin not found: openclaw-hermes-router (stale config entry ignored)`

## 10. Exact Implementation Prerequisites

1. **Create plugin package structure:**
   ```
   tools/openclaw-hermes-router-plugin/
     package.json
     openclaw.plugin.json
     tsconfig.json
     src/index.ts
     dist/ (built output)
   ```

2. **Implement tool:**
   - Tool name: `invoke_hermes`
   - Parameters: `message` (string, required)
   - Execute: spawn `hermes chat -q <message>`, capture output, return OpenClaw tool-result format

3. **Build:**
   - `npm install` (typebox dep)
   - `npm run build` (tsc)
   - `openclaw plugins build --entry ./dist/index.js`
   - `openclaw plugins validate --entry ./dist/index.js`

4. **Install:**
   - `openclaw plugins install --link ./tools/openclaw-hermes-router-plugin`

5. **Configure:**
   - Update `plugins.entries.openclaw-hermes-router` to point to installed plugin path
   - Ensure `tools.allow` includes `invoke_hermes` or use appropriate profile

6. **Restart Gateway:**
   - `openclaw gateway restart`
   - Verify with `openclaw plugins inspect openclaw-hermes-router --runtime --json`

7. **Test:**
   - Send Telegram message to OpenClaw
   - Verify OpenClaw agent invokes `invoke_hermes`
   - Verify Hermes response returns through OpenClaw to Telegram

## 11. Whether C1 Can Realistically Be Resolved Without Violating the Constitution

**Yes, but with a caveat.**

The locked architecture says:
> OpenClaw receives and routes user interactions. It may delegate to Hermes.

A tool plugin that invokes Hermes is a supported delegation mechanism. It preserves:
- Hermes as the primary reasoning brain
- OpenClaw as communication gateway
- Telegram as human interface
- Specialist agents as bounded processes

**Caveat:** The delegation is agent-discretionary, not deterministic. OpenClaw's model decides when to invoke Hermes. The architecture document implies all messages route to Hermes, but no supported mechanism enforces deterministic all-message routing without a custom channel plugin or unsupported hook manipulation.

If deterministic routing is strictly required, the next phase would need to explore:
- A custom channel plugin that wraps telegram dispatch (heavyweight, architecture-risk)
- Or accept agent-discretionary routing as the supported interpretation of "may delegate"

## 12. Confidence Level

**Medium-High** that a tool plugin is the correct supported mechanism.

**Low** that deterministic all-message routing is achievable through supported mechanisms without architectural changes.

## 13. Explicit Blockers/Unknowns

### Blockers
1. **Plugin packaging contract:** The repo-side router must be rebuilt as a conformant OpenClaw plugin package. This is straightforward but requires implementation.
2. **Deterministic vs discretionary routing:** No supported mechanism guarantees every Telegram message invokes Hermes. The tool-plugin path relies on the agent/model's tool selection.

### Unknowns
1. **Tool selection reliability:** Will OpenClaw's model reliably call `invoke_hermes` for every inbound Telegram message, or only when it deems the message relevant? This depends on the system prompt and model behavior.
2. **Plugin build toolchain:** The plugin requires TypeScript build steps (`tsc`, `openclaw plugins build`). These have not been executed in this investigation.
3. **Gateway restart behavior:** The Sep 4 SQLite worker failure may recur on restart. The worker succeeds when run manually, suggesting a Windows scheduled-task environment issue. This needs monitoring during plugin install.
4. **Plugin registry/state:** `openclaw plugins install` writes to a SQLite-backed registry. The existing stale `openclaw-hermes-router` entry may need cleanup before the new plugin installs cleanly.

---

*END OF INVESTIGATION*
