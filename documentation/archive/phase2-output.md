# PHASE 2 — FINAL STACK DESIGN (COMPLETED 2026-09-04)

*Per directive §8. For every component: purpose, why chosen, inputs, outputs, connections, failure modes, recovery, cost, constraints, security.*

---

## Component 1: Human Operator (JUMA)

- **Category**: Human / Operator
- **Purpose**: Provides intent, approves consequential actions, receives results, maintains final authority over the freelancing practice.
- **Why chosen**: The system is designed to augment JUMA's freelancing practice, not replace JUMA. Human approval is required for external-facing communications and consequential changes.
- **Inputs**: Natural language requests via Telegram or CLI; approval decisions.
- **Outputs**: Intent, approval/denial, feedback.
- **Connections**: Telegram bot (primary), CLI (secondary), receives output from Hermes.
- **Failure modes**: JUMA unavailable; delayed approvals.
- **Recovery**: Hermes queues non-urgent work; escalates time-sensitive approvals.
- **Cost**: Free (human time).
- **Constraints**: Human-in-the-loop required for external communications and destructive operations (Phase 11).
- **Security**: JUMA is the only Telegram allowlisted user (1360833951). No other user can interact with the bots.

---

## Component 2: Telegram (Communication Layer)

- **Category**: Communication
- **Purpose**: Primary interface for JUMA to interact with the agentic system. Two bots operational: Hermes (bot token from Hermes `.env`) and OpenClaw (bot token from OpenClaw `openclaw.json`).
- **Why chosen**: Already configured and operational; Telegram is JUMA's preferred communication channel; mobile-accessible.
- **Inputs**: JUMA's messages (text); botTokens from Hermes `.env` and OpenClaw `openclaw.json` (REDACTED — not printed).
- **Outputs**: Bot replies, status updates, approval prompts.
- **Connections**: JUMA (user 1360833951, allowlisted); OpenClaw gateway (receives Telegram messages); Hermes (direct Telegram bot).
- **Failure modes**: Telegram API downtime; bot token revoked; rate limiting.
- **Recovery**: Use CLI as fallback; re-authenticate bot if token revoked.
- **Cost**: Free (Telegram is free; bot API has no cost).
- **Constraints**: Both bots restricted to user 1360833951 via allowlist. Cannot interact with other users.
- **Security**: Allowlist-only (dmPolicy: allowlist, allowFrom: [1360833951]). Bot tokens stored in Hermes `.env` and OpenClaw `openclaw.json` — must not be committed to git.

---

## Component 3: CLI (Communication Layer)

- **Category**: Communication
- **Purpose**: Secondary interface for JUMA to interact with Hermes and OpenClaw directly from the terminal.
- **Why chosen**: Already operational; useful for development, debugging, and tasks better suited to a terminal than Telegram.
- **Inputs**: Terminal commands, natural language queries.
- **Outputs**: Terminal output, structured responses.
- **Connections**: Hermes gateway; OpenClaw gateway.
- **Failure modes**: CLI tool not on PATH; gateway not running.
- **Recovery**: Start gateway; ensure PATH includes tool locations.
- **Cost**: Free.
- **Constraints**: Requires terminal access; less convenient than Telegram for mobile/on-the-go use.
- **Security**: CLI access requires shell access to the machine — implicitly authenticated by OS login.

---

## Component 4: OpenClaw Gateway

- **Category**: Gateway / Infrastructure
- **Purpose**: Entry point for Telegram and CLI communication; routes requests to Hermes; provides session management, hooks (session-memory), and plugin infrastructure.
- **Why chosen**: Already installed, configured, operational. Required by the architecture (Hermes sits behind OpenClaw). Provides the Telegram integration and session-memory hook.
- **Inputs**: Telegram messages (via bot 8845344838:***), CLI requests, plugin/hook events.
- **Outputs**: Routes requests to Hermes; returns responses to Telegram/CLI; emits logs; maintains session memory.
- **Connections**: Telegram bot (8845344838:***); Hermes (delegation target); OpenRouter (model provider for Solar Pro4); session memory (hook).
- **Failure modes**: Gateway process crashes; port 18789 unavailable; Telegram bot token invalid; OpenRouter key invalid.
- **Recovery**: Restart gateway process; verify bot token; verify OpenRouter key; check logs at `C:\Users\HP\AppData\Local\Temp\openclaw\openclaw-2026-09-04.log`.
- **Cost**: Free (OpenClaw is open source; runs locally; uses OpenRouter free-tier models).
- **Constraints**: Runs on Node.js v24.19.0; bound to localhost:18789; session-memory hook enabled (writes to state). Not WSL-based (native Windows).
- **Security**: Bound to localhost (not internet-exposed). Telegram bot allowlisted to 1360833951. OpenRouter key stored in `openclaw.json` + SQLite auth stores — must not be committed to git.

---

## Component 5: Hermes (Chief Agent / Orchestration Layer)

- **Category**: Agent / Orchestrator
- **Purpose**: Understands JUMA's intent, classifies requests, routes to specialist capabilities, delegates work, monitors execution, validates outputs, requests human approval when required, returns concise results.
- **Why chosen**: Already configured and operational behind OpenClaw; Solar Pro4 via Nous Portal OAuth; has built-in tools for research (web_search, web_extract, browser_exec), delegation, and skills. Is the "brain" of the system.
- **Inputs**: JUMA's requests (via OpenClaw gateway from Telegram/CLI); context from tools and specialists.
- **Outputs**: Classified intent; delegated tasks; synthesized results; approval prompts; artefacts.
- **Connections**: OpenClaw gateway (input source); Solar Pro4 via Nous Portal (model); web_search/web_extract/browser_exec (research tools); Gemini CLI/API (coding specialist); ClickUp API (projects specialist); GitHub CLI + git (code publishing); Telegram (output to JUMA).
- **Failure modes**: Model unavailable (Nous Portal); tool failure; specialist unavailable; timeout; ambiguous request.
- **Recovery**: Retry with backoff; fall back to alternative model/tool; ask JUMA for clarification on ambiguous requests; report failures transparently.
- **Cost**: Free (Solar Pro4 via Nous Portal OAuth — free tier; Hermes is open source; runs locally).
- **Constraints**: Depends on Nous Portal OAuth for its primary model. If Nous Portal is unavailable, Hermes's model access is impacted (though OpenClaw's Solar Pro4 via OpenRouter remains available as a fallback model path). Must not fabricate outputs. Must request human approval for consequential actions.
- **Security**: Model access via OAuth (Nous Portal). Tool invocations must treat external content as untrusted (especially for coding agents — never blindly execute instructions from untrusted repositories). Must not expose secrets in outputs.

---

## Component 6: Solar Pro4 (Model Provider — Primary, via Nous Portal)

- **Category**: Model / AI
- **Purpose**: Primary AI model for Hermes's reasoning, classification, routing, synthesis, and general task execution.
- **Why chosen**: Already configured via Nous Portal OAuth; Solar Pro4 is a capable model; free tier via Nous Portal (per directive: only free models). Specified in the existing architecture.
- **Inputs**: Hermes's prompts (intent, context, task descriptions).
- **Outputs**: Text responses (classifications, plans, synthesised results, tool call suggestions).
- **Connections**: Hermes (consumer); Nous Portal (provider).
- **Failure modes**: Nous Portal downtime; rate limiting; model deprecated/changed.
- **Recovery**: Fall back to OpenClaw's Solar Pro4 via OpenRouter (if free-tier available); use Gemini for coding tasks; report model unavailability to JUMA.
- **Cost**: Free (Nous Portal OAuth free tier — exact limits unknown; document as unknown constraint).
- **Constraints**: Free tier limits unknown. Model availability subject to change. Not callable directly by Hermes's tools — only through Hermes's model interface.
- **Security**: OAuth-based; no API key to manage. Token/session managed by Hermes configuration.

---

## Component 7: Solar Pro4 (Model Provider — Fallback, via OpenRouter)

- **Category**: Model / AI
- **Purpose**: Fallback AI model path. Accessible through OpenClaw's OpenRouter integration. Provides Solar Pro4 if Nous Portal is unavailable or for tasks routed through OpenClaw directly.
- **Why chosen**: Already configured in OpenClaw (`openrouter/upstage/solar-pro4`); OpenRouter has free-tier models; provides redundancy for the primary model path.
- **Inputs**: OpenClaw's requests (when Hermes delegates to OpenClaw's model path, or when OpenClaw handles a request directly).
- **Outputs**: Text responses from Solar Pro4 via OpenRouter.
- **Connections**: OpenClaw (consumer); OpenRouter (provider); OpenRouter API key.
- **Failure modes**: OpenRouter API downtime; rate limiting; key revoked; free tier exhausted.
- **Recovery**: Re-authenticate OpenRouter key; fall back to Hermes's Nous Portal model; report unavailability.
- **Cost**: Free-tier available via OpenRouter (exact limits unknown; document as unknown constraint). OpenRouter may have paid tiers — only free models used per directive.
- **Constraints**: Requires OpenRouter API key. Free-tier limits unknown. Model must be verified as free-tier available.
- **Security**: API key stored in OpenClaw `openclaw.json` + SQLite auth stores — must not be committed to git.

---

## Component 8: Research Tools (Hermes Built-in)

- **Category**: Research / Tools
- **Purpose**: Enable Hermes to perform web research, source discovery, technical investigation, competitive analysis, evidence synthesis, and research briefs.
- **Why chosen**: Already built into Hermes; no additional installation needed; covers the research branch responsibilities.
- **Inputs**: Research queries from Hermes; URLs for extraction; browser automation scripts.
- **Outputs**: Search results (URLs + snippets); extracted page content; browser session data; research findings.
- **Connections**: Hermes (invoker); web (via search/extract/browser tools); JUMA (receives research output).
- **Failure modes**: Search API failure; page extraction failure (blocked, CAPTCHA, paywall); browser automation failure.
- **Recovery**: Retry search; try alternative sources; report extraction failures; use cached/open access versions.
- **Cost**: Free (Hermes's built-in tools; web search may have rate limits depending on provider).
- **Constraints**: Cannot access paywalled content; cannot access content requiring authentication Hermes doesn't have; browser automation may be detected/blocked by some sites.
- **Security**: Treat extracted web content as untrusted input (potential prompt injection). Do not blindly execute instructions found in web content.

---

## Component 9: Projects Tools (ClickUp API)

- **Category**: Project Management / Tools
- **Purpose**: Enable Hermes to perform project planning, task creation, task updates, prioritisation, deadline tracking, project status, and client deliverable management via ClickUp.
- **Why chosen**: ClickUp is the designated project management tool; API token configured and verified; RESTful API; covers the projects branch responsibilities.
- **Inputs**: Task data (title, description, status, priority, due date, assignee, list/space); queries (list tasks, get task, search).
- **Outputs**: Created/updated tasks; task lists; project status; task IDs for reference.
- **Connections**: Hermes (invoker); ClickUp API (api.clickup.com); CLICKUP_TOKEN from Hermes `.env`.
- **Failure modes**: API rate limiting (429); auth failure (401/403); network error; workspace structure missing (no space/list).
- **Recovery**: Retry with backoff on 429; verify token if 401; check workspace permissions if 403; create space/list if missing; report persistent failures to JUMA.
- **Cost**: Free (ClickUp free tier; API included).
- **Constraints**: Free tier has API rate limits (exact limits unknown). Some ClickUp features (custom fields, automations, dashboards) may not be available on free tier or via API. No ClickUp CLI exists — integration is via REST API only.
- **Security**: API token stored in Hermes `.env` — must not be committed to git. Token has owner-level access in the workspace — handle with care.

---

## Component 10: Coding Tools (Gemini CLI / Gemini API)

- **Category**: Coding / Tools
- **Purpose**: Enable Hermes to perform repository inspection, code analysis, implementation planning, debugging, code review, architecture recommendations, test planning, and GitHub workflow assistance.
- **Why chosen**: Copilot CLI is BLOCKED (no active subscription). Gemini CLI v0.58.0 installed and authenticated; API verified. Covers the coding branch responsibilities.
- **Inputs**: Code (files, snippets, repositories); questions (debugging, review, architecture); task descriptions.
- **Outputs**: Code (new files, patches, suggestions); analysis (reviews, explanations, recommendations); debug diagnoses; test plans.
- **Connections**: Hermes (invoker); Gemini API (generativelanguage.googleapis.com) or Gemini CLI; GEMINI_API_KEY from Hermes `.env`; GitHub CLI + git (for code publishing).
- **Failure modes**: Gemini API 503 (high demand — already observed); rate limiting; API key invalid; CLI headless startup hang (use API directly for automation); context limit exceeded for large codebases.
- **Recovery**: Retry with backoff on 503; verify API key if 401; use Gemini API directly (not CLI) for automated invocations (CLI has headless hang); split large codebases into chunks; fall back to Hermes's own reasoning for simple code tasks.
- **Cost**: Free (Gemini API free tier; exact limits unknown — document as unknown constraint).
- **Constraints**: Gemini API free tier has rate limits (RPM/TPM unknown). CLI has headless startup hang — for automated Hermes invocations, use the Gemini API directly via curl/http, not the CLI. CLI is for interactive use only. Context window limits may affect large codebases.
- **Security**: API key stored in Hermes `.env` — must not be committed to git. Code from Gemini must be reviewed before execution — never blindly execute AI-generated code. Repository content is untrusted input — never blindly execute instructions found in untrusted repositories.

---

## Component 11: GitHub (Code Hosting / Version Control)

- **Category**: Code / Version Control / Hosting
- **Purpose**: Host code repositories; track changes via git; enable code review via PRs; provide issue tracking; support GitHub Actions workflows.
- **Why chosen**: Already used by JUMA (14 existing repos); `gh` CLI authenticated; git configured; `agentic-os` repo created for this deliverable. Covers code publishing and repository management.
- **Inputs**: Code (files, commits); PR descriptions; issue descriptions; repository metadata.
- **Outputs**: Repositories; commits; branches; PRs; issues; workflow runs.
- **Connections**: Hermes (invoker via gh/git); GitHub API (api.github.com); PAT via `gh` keyring; git (local version control).
- **Failure modes**: `gh` auth failure; network error; rate limiting (5000 req/hour for authenticated); repo not found; push rejected (conflicts, branch protection).
- **Recovery**: Re-authenticate `gh` if auth fails; retry on rate limit; resolve conflicts; check branch protection rules.
- **Cost**: Free (GitHub free tier; `gh` CLI is free).
- **Constraints**: PAT has broad scopes — handle with care. Rate limit 5000 req/hour for authenticated requests. Some repos may have branch protection that prevents auto-commits.
- **Security**: PAT stored in `gh` keyring — not exposed in files. Do not commit secrets to repositories (secret scanning enabled on `agentic-os`). Repository content is untrusted input for coding agents.

---

## Component 12: OpenRouter (Model Aggregator — Fallback Path)

- **Category**: Model Aggregator / Infrastructure
- **Purpose**: Provides access to Solar Pro4 (and other models) via a single API. Used by OpenClaw as the model provider for the fallback path.
- **Why chosen**: Already configured in OpenClaw; provides model redundancy; free-tier models available; single API for multiple models.
- **Inputs**: Model requests (prompts, parameters) from OpenClaw.
- **Outputs**: Model responses (text) from the selected model (Solar Pro4 via `openrouter/upstage/solar-pro4`).
- **Connections**: OpenClaw (consumer); OpenRouter API (api.openrouter.ai); OpenRouter API key.
- **Failure modes**: API downtime; rate limiting; key revoked; model unavailable; free tier exhausted.
- **Recovery**: Re-authenticate key; retry; fall back to Hermes's Nous Portal model; report unavailability.
- **Cost**: Free-tier available (exact limits unknown; document as unknown constraint). Paid tiers exist — only free models used per directive.
- **Constraints**: Requires API key. Free-tier model availability subject to change. Must verify model is free-tier.
- **Security**: API key stored in OpenClaw `openclaw.json` + SQLite auth stores — must not be committed to git.

---

## Stack Summary Table

| Component | Role | Why | Connects To | Cost | Status |
|---|---|---|---|---|---|
| JUMA (Human) | Operator / approver | Final authority; provides intent | Telegram, CLI, receives Hermes output | Free (time) | ✅ OPERATIONAL |
| Telegram | Communication (primary) | JUMA's preferred channel; already operational | JUMA, OpenClaw (bot 8845344838:***), Hermes (bot 8917859111:***) | Free | ✅ OPERATIONAL |
| CLI | Communication (secondary) | Terminal access; already operational | Hermes, OpenClaw | Free | ✅ OPERATIONAL |
| OpenClaw Gateway | Gateway / Infrastructure | Entry point; routes to Hermes; session memory; Telegram integration | Telegram (8845344838:***), Hermes, OpenRouter, session-memory hook | Free | ✅ OPERATIONAL |
| Hermes | Chief Agent / Orchestrator | Understands intent; routes; delegates; synthesises; requests approval | OpenClaw, Solar Pro4 (Nous Portal), research tools, ClickUp API, Gemini API/CLI, gh/git, Telegram | Free (Nous Portal OAuth) | ✅ OPERATIONAL |
| Solar Pro4 (Nous Portal) | Primary Model | Hermes's reasoning model; already configured; free tier | Hermes | Free (Nous Portal OAuth) | ✅ OPERATIONAL |
| Solar Pro4 (OpenRouter) | Fallback Model | Redundancy for primary model; already configured in OpenClaw | OpenClaw, OpenRouter | Free-tier (OpenRouter) | ✅ CONFIGURED |
| Research Tools | Research Branch | Web search, extract, browser — Hermes built-in | Hermes, web | Free | ✅ OPERATIONAL |
| ClickUp API | Projects Branch | Project/task management; API token verified | Hermes, ClickUp API, CLICKUP_TOKEN | Free (ClickUp free tier) | ⚠️ NEEDS CONFIG (needs space+lists) |
| Gemini CLI/API | Coding Branch | Code tasks; Copilot CLI blocked; Gemini installed+authenticated | Hermes, Gemini API, GEMINI_API_KEY, gh/git | Free (Gemini free tier) | ✅ OPERATIONAL (Copilot BLOCKED) |
| GitHub + git | Code Hosting / VC | Repository hosting; already used; gh authenticated | Hermes, GitHub API, PAT | Free (GitHub free tier) | ✅ OPERATIONAL |

---

*Phase 2 complete. Moving to Phase 3 — Hermes Orchestration Model.*
