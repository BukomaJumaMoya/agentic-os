# Phase 8 — Security Review

*Per directive §18. Every credential, integration, and communication channel must be reviewed for security posture.*

---

## 8.1 Credential Inventory

**Credentials in use (all redacted in tracked files):**

| Credential | Value (redacted) | Storage | Exposure Risk |
|------------|------------------|---------|---------------|
| OpenRouter API key | `sk-or-...f8c1` (73 chars) | OpenClaw state DB (`openclaw.sqlite`) + agent DB (`openclaw-agent.sqlite`) + `openclaw.json` auth.profiles | Medium — stored in three SQLite/openhwa files; accessible to anyone with filesystem access |
| Telegram bot token (Hermes) | `8917859111:***` | Hermes `.env` (`C:\Users\HP\AppData\Local\hermes\.env`) | Low — `.env` not tracked in git; accessible to HP user account |
| Telegram bot token (OpenClaw) | `8845344838:***` | OpenClaw config (`C:\Users\HP\.openclaw\openclaw.json`) | Medium — stored in plain JSON config file |
| Gemini API key | `AQ.Ab...` (53 chars) | Hermes `.env` (`C:\Users\HP\AppData\Local\hermes\.env`) + generates proposal drafts | Low — `.env` not tracked; Gemini key has generative content scope only |
| ClickUp token | `pk_240010007_...` (49 chars) | Hermes `.env` + passed to `clickup.js` via env var | Low — `.env` not tracked; token has full workspace access (create/read/update tasks) |
| GitHub PAT | `ghp_...` | gh keyring (Windows credential manager) | Low — stored in OS credential manager; PAT has repo read/write scope |
| OpenClaw gateway auth token | `c5e8c32e050a...663111` | OpenClaw config (`openclaw.json`) | Low — only used for local API auth (127.0.0.1); not exposed externally |

**Credentials NOT stored in git-tracked files:** All secrets live in `.env`, SQLite DBs, or `openclaw.json` — none of which are committed to the git repo `BukomaJumaMoya/agentic-os`.

**Credentials verified clean in git:** `git grep` for `sk-or`, `pk_24001`, `AQ.Ab`, `ghp_`, `891785`, `884534` returns zero matches in HEAD.

---

## 8.2 Access Control

### Telegram Bots

**Hermes Telegram bot (`8917859111:***`):**
- `TELEGRAM_ALLOWED_USERS=1360833951` — only user ID 1360833951 can interact
- Configured via Hermes gateway config (`config.yaml`): `gateway.platforms.telegram.allow_list: [1360833951]`
- Verified: inbound message from Juma Moya (ID 1360833951) received and replied to; no other users can reach the bot

**OpenClaw Telegram bot (`8845344838:***`, `@bukomaopenclawbot`):**
- `channels.telegram.dmPolicy: "allowlist"` — deny by default
- `channels.telegram.allowFrom: ["1360833951"]` — only user ID 1360833951 can interact
- Configured in `openclaw.json`, hot-reloaded by gateway
- Verified: bot replies to Juma Moya; polling active at offset 604676639

**Both bots restrict access to a single Telegram user ID (1360833951).**

### OpenClaw Gateway API

- `gateway.auth.mode: "token"` — token-based auth for local API
- Gateway binds to `127.0.0.1:18789` (loopback only — not exposed on network)
- Auth token `c5e8c32e050a...663111` required for all API calls
- No external network exposure — only accessible from localhost

### ClickUp API

- Token `pk_240010007_...` is a personal token (owner: Juma Moya, ID 240010007)
- Token has full workspace access (create/read/update tasks in Freelance space)
- Token passed via environment variable (not hardcoded in scripts)
- ClickUp API calls go to `api.clickup.com` over HTTPS

### Gemini API

- Key `AQ.Ab...` is a Google AI Studio API key
- Key has generative content scope (generativelanguage.googleapis.com)
- Key passed via environment variable (not hardcoded in scripts)
- Gemini API calls go to `generativelanguage.googleapis.com` over HTTPS

### GitHub

- PAT `ghp_...` stored in Windows credential manager (via `gh auth login --with-token`)
- PAT has repo scope (read/write to `BukomaJumaMoya/agentic-os` and other repos)
- `gh` CLI uses the stored PAT for authentication
- Git operations use `git config credential.helper "!gh auth git-credential"` to pipe through gh

---

## 8.3 Network Security

**Outbound connections (all HTTPS):**
- `api.openrouter.ai` — OpenRouter API (Solar Pro4 inference)
- `generativelanguage.googleapis.com` — Gemini API
- `api.clickup.com` — ClickUp REST API
- `api.github.com` — GitHub API (via gh CLI)
- `api.telegram.org` — Telegram Bot API (polling, not webhooks)

**Inbound connections:**
- `127.0.0.1:18789` — OpenClaw gateway (loopback only, auth required)

**No inbound ports exposed on the network.** All services are either outbound-only (APIs) or loopback-only (gateway).

---

## 8.4 Security Issues Identified

### Issue 1: OpenClaw auth key stored in plain JSON
**Severity:** Medium
**Description:** The OpenRouter API key is stored in `openclaw.json` under `auth.profiles.openrouter:default` as a plain JSON value, AND in two SQLite databases. Any process running as the HP user can read these files.
**Mitigation:** The key is only accessible to the HP user account. The gateway binds to loopback only. The machine is a personal laptop, not a shared server. Acceptable risk for current use case.
**Recommendation:** Consider using OpenClaw's encrypted auth store if available in future versions. For now, restrict filesystem access to the HP user account only.

### Issue 2: ClickUp token has broad permissions
**Severity:** Medium
**Description:** The ClickUp personal token has full workspace access — it can create, read, update, and delete any task in the Freelance space. If compromised, an attacker could modify task data.
**Mitigation:** Token is stored in `.env` (not tracked in git). Only the `clickup.js` script and `generate-proposal.ps1` use it. Both scripts are in the git repo (code is public) but the token itself is not.
**Recommendation:** Consider creating a ClickUp API token with narrower scope (only the Freelance space, read/write tasks only) if ClickUp supports scoped tokens. Currently, ClickUp personal tokens have workspace-wide access.

### Issue 3: Gemini key used in proposal automation
**Severity:** Low
**Description:** The Gemini API key is used by `generate-proposal.ps1` to generate proposal drafts. The key is passed via environment variable and used in a single API call per proposal.
**Mitigation:** Key is stored in `.env` (not tracked). The proposal script is run manually (not automated on a schedule). Each invocation uses the key once.
**Recommendation:** Acceptable for current use. If automation becomes frequent, consider rotating the key periodically or using a dedicated Gemini key with only generative content scope.

### Issue 4: GitHub PAT stored in gh keyring
**Severity:** Low
**Description:** The GitHub PAT is stored in the Windows credential manager via `gh auth login`. The PAT has repo read/write scope.
**Mitigation:** PAT is not stored in any file on disk (only in Windows credential manager). `gh` CLI handles authentication. Git operations use `gh auth git-credential` as the credential helper.
**Recommendation:** Acceptable. The PAT should be rotated if the laptop is compromised. Consider using a fine-grained PAT with repo-specific scope instead of the current personal token.

### Issue 5: Telegram bot tokens visible in config files
**Severity:** Low
**Description:** Both Telegram bot tokens are stored in plain text in config files (`.env` for Hermes, `openclaw.json` for OpenClaw). If either file is read by an attacker, they can impersonate the bot.
**Mitigation:** `.env` is in `.gitignore` (not tracked). `openclaw.json` is not tracked in the git repo (it's in `~/.openclaw/`). Both files are only accessible to the HP user account.
**Recommendation:** Telegram bot tokens can be regenerated at any time via @BotFather if compromise is suspected. No action needed unless tokens are exposed.

---

## 8.5 Security Posture Summary

| Aspect | Status | Notes |
|--------|--------|-------|
| Credentials in git | ✅ CLEAN | Zero secrets in tracked files (verified via `git grep`) |
| Credential storage | ⚠️ MIXED | `.env` (good), SQLite DBs (acceptable), `openclaw.json` (acceptable for personal use) |
| Access control | ✅ RESTRICTIVE | Both Telegram bots allowlist only user 1360833951 |
| Network exposure | ✅ MINIMAL | Only outbound HTTPS + loopback gateway; no inbound ports |
| Auth enforcement | ✅ ENFORCED | OpenClaw gateway requires token; Telegram bots enforce allowlist |
| Credential rotation | ⚠️ MANUAL | All credentials require manual rotation if compromised |
| Audit trail | ✅ PRESENT | All operations logged (see Phase 7 — Observability) |

**Overall assessment:** The security posture is appropriate for a personal freelance AI stack running on a single-user laptop. No critical vulnerabilities identified. The main improvement opportunity is migrating credential storage to encrypted stores where available (future work).

**Recommendations for future hardening:**
1. Use OpenClaw's encrypted auth store if/when available
2. Create scoped ClickUp API tokens (space-level, task-only)
3. Rotate GitHub PAT to a fine-grained token with repo-specific scope
4. Enable Windows BitLocker for full-disk encryption (if not already enabled)
5. Set up credential rotation reminders (e.g., every 90 days for API keys)
