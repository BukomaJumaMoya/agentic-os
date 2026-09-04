# HERMES DELIVERABLE 1 — Phase 5: Coding Agent Definition

**Date**: 2026-09-04  
**Status**: Phase 5 COMPLETE — coding capability defined; Gemini is the coding agent (Copilot substituted)

---

## 1. ROLE

The Coding Agent is **not a separate process** — it's a **Hermes capability** that delegates code tasks to Gemini (CLI or API). Hermes invokes it when the request is code-related.

Coding handles:
- Generating code (new files, scripts, functions, components, entire projects)
- Reviewing code (finding bugs, suggesting improvements, checking style)
- Debugging (analyzing errors, suggesting fixes, explaining stack traces)
- Explaining code (what a piece of code does, how it works)
- Refactoring / porting (converting from one language/framework to another)
- Writing scripts (automation scripts, dev scripts, build scripts)
- Library / technology recommendations (best library for X, which approach to use)
- Writing tests (unit tests, integration tests, test structures)
- Writing documentation for code (comments, docstrings, READMEs)

---

## 2. CONNECTOR

**Gemini** — the primary coding model.

**Two invocation paths:**

### 2.1 Gemini REST API (primary, reliable for automation)

- Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}`
- Method: POST, JSON body with `contents[].parts[].text`
- Response: JSON with `candidates[].content.parts[].text`
- Auth: `GEMINI_API_KEY` from Hermes `.env` (set in Hermes `.env`; key starts with `AQ.Ab...`)
- **Verified live**: curl test returned HTTP 200 with real response text (`FULLY AUTHENTICATED AND READY TO WORK`)
- **Model**: `gemini-3.6-flash` (the CLI/API default; `gemini-2.5-flash` is no longer available to new users per the API's 404 response)
- **Why API over CLI for automation**: The Gemini CLI has a headless startup hang (grep/tooling init or interactive-mode detection) — the API is reliable, fast, and non-interactive.

### 2.2 Gemini CLI (interactive, when user wants to iterate)

- Command: `gemini --prompt "..." --skip-trust --model gemini-3.6-flash`
- When to use: When the user wants to interact with Gemini directly (e.g., "spin up Gemini and let me work with it")
- Trust flag: `--skip-trust` (or `GEMINI_CLI_TRUST_WORKSPACE=true` in env)
- Auth: Same API key (via env `GEMINI_API_KEY`)
- **Known issue**: CLI in headless/non-interactive mode has a startup hang — not suitable for automated Hermes delegation; only use for interactive user sessions.

---

## 3. CAPABILITIES — WHAT HERMES DELEGATES TO GEMINI

| Task | What Hermes sends to Gemini | What Gemini returns |
|---|---|---|
| Generate code | The request + any constraints (language, framework, style, requirements) | Code (one or more files) |
| Review code | The code + what to look for (bugs, style, performance, security) | Review findings + suggestions |
| Debug error | The error message + code context + what was tried | Diagnosis + fix suggestions |
| Explain code | The code + what level of explanation (beginner, expert, line-by-line) | Explanation |
| Refactor / port | The code + target language/framework + constraints | Refactored code |
| Write a script | The script's purpose + language + any requirements | Script code |
| Recommend library | The problem + constraints (language, ecosystem, preferences) | Recommendations + reasoning |
| Write tests | The code + test framework + what to test | Test code |
| Document code | The code + desired format (comments, docstrings, README) | Documentation |

---

## 4. HOW HERMES USES CODING OUTPUT

1. Hermes sends the code task to Gemini (via API or CLI).
2. Gemini returns code/output.
3. Hermes passes the output to the user, along with any context (file paths, how to run it, caveats).
4. If the code needs to be saved to files, Hermes writes the files (or uses Gemini's file-writing capability if available).
5. If the code needs to be committed/pushed to GitHub, Hermes uses `git` + `gh` (Phase 6 connection patterns).
6. Hermes verifies the output against the user's intent before presenting it ("Does this match what you asked for? Here's what I got...").

---

## 5. OUTPUT FORMAT

Coding output from Hermes should include:
- The code itself (in code blocks, with file paths if multiple files)
- What the code does (brief explanation)
- How to run / use it (dependencies, commands, setup)
- Any caveats or known limitations
- What was NOT done (if the request was ambiguous or partial)

---

## 6. LIMITS

- **Gemini API rate limits**: Free tier via Google AI Studio has RPM/TPM limits (exact limits unknown; Google AI Studio dashboard shows quotas). Sustained coding use may hit limits.
- **Model capability**: `gemini-3.6-flash` is the available model — good for code generation and review, but very large codebases or highly specialized domains may exceed its context or knowledge.
- **CLI headless hang**: The Gemini CLI cannot be reliably used for automated Hermes delegation in headless mode — use the API instead. The CLI is for interactive user sessions only.
- **No file system access from Hermes delegation**: When Hermes delegates to Gemini via API, Gemini generates text (code). Hermes is responsible for writing that code to files if needed. Gemini doesn't write files directly in the API flow.
- **Copilot unavailable**: Copilot CLI (1.0.82) is installed but unusable without an active GitHub Copilot subscription. Gemini is the substitute. If the user activates a Copilot subscription later, Copilot can be re-evaluated — but Gemini is the current coding agent.

---

## 7. WHEN NOT TO USE CODING (HERMES DIRECT)

- Questions that don't involve code → Hermes direct (Solar Pro4 via Nous Portal)
- Research / web search → Hermes research capability (Phase 3)
- Project/task management → ClickUp (Phase 4)
- General analysis that Hermes can do → Hermes direct
- Anything requiring human judgment → ask user (Phase 17)

---

## 8. CODING WORKFLOW (TYPICAL)

```
User: "Write a Python script that does X"
     │
     ▼
Hermes: Route to Gemini (coding task)
     │
     ▼
Hermes → Gemini API: "Write a Python script that does X. Requirements: ..."
     │
     ▼
Gemini API → returns code
     │
     ▼
Hermes: Present code to user + explanation + how to run
     │
     ▼
If user wants it saved:
     │
     ▼
Hermes: Write code to file(s) in project dir
     │
     ▼
If user wants it in GitHub:
     │
     ▼
Hermes: git add + commit + gh push (Phase 6)
```

---

*Phase 5 complete. Coding capability defined; Gemini (API + CLI) is the coding agent; Copilot documented as unavailable.*
