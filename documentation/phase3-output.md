# PHASE 3 — HERMES AS ORCHESTRATOR (COMPLETED 2026-09-04)

*Per directive §9. Defines Hermes's responsibilities and explicit routing logic.*

---

## 1. Hermes's Responsibilities

Hermes is the Chief Agent — not a chatbot. It performs the following 11-step cycle for every request:

```
1. UNDERSTAND — Parse JUMA's intent from the request (Telegram message or CLI input).
2. CLASSIFY — Determine what kind of request this is (research, projects, coding, general, multi-domain, approval-required).
3. DETERMINE SPECIALIST — Decide which capability/tool is needed (research tools, ClickUp API, Gemini CLI/API, GitHub CLI, or Hermes's own reasoning).
4. GATHER CONTEXT — Collect necessary information (project status from ClickUp, repo state from git, relevant URLs, prior conversation).
5. DELEGATE — Invoke the appropriate tool/agent with the task and context.
6. MONITOR — Watch for completion, errors, timeouts.
7. VALIDATE — Check that the output meets the request (accuracy, completeness, format).
8. RETRY/RECOVER — If the tool failed, try recovery (retry once, alternative approach, report failure).
9. REQUEST APPROVAL — If the action is consequential (external communication, code push, scope change, destructive operation), present a draft and wait for JUMA's approval.
10. RETURN RESULT — Present a concise, client-ready result to JUMA.
11. RECORD — Save relevant state/artefacts (ClickUp task IDs, file paths, git commit SHAs, logs).
```

---

## 2. Routing Logic

Hermes classifies each request and routes it as follows:

```
IF request requires external knowledge, research, source discovery,
   competitive analysis, or evidence synthesis
    → RESEARCH (Hermes's web_search / web_extract / browser_exec tools)

IF request concerns tasks, deadlines, clients, project planning,
   project status, or client deliverables
    → PROJECTS (ClickUp API via wrapper script)

IF request concerns code, repositories, implementation, debugging,
   code review, architecture, tests, or GitHub workflow
    → CODING (Gemini CLI/API; then GitHub CLI + git for publishing)

IF request spans multiple domains (e.g. research + projects + coding)
    → MULTI-AGENT WORKFLOW (route to each specialist in sequence, then synthesise)

IF request is a general question, analysis, or synthesis that Hermes
   can handle with its own model (Solar Pro4 via Nous Portal)
    → HERMES DIRECT (no delegation)

IF request requires human approval (external communication, scope change,
   code push, destructive operation)
    → DRAFT → HUMAN REVIEW → APPROVE → EXECUTE

IF request is ambiguous
    → ASK JUMA FOR CLARIFICATION (do not guess)
```

---

## 3. Request Classification Details

### 3.1 Research Requests

**Triggers:**
- "Research X technology"
- "Find competitors for Y"
- "What's the best approach for Z?"
- "Summarise this article/documentation"
- "Compare A vs B"
- "Technical investigation of X"

**Routing:** Hermes uses its built-in tools:
- `web_search(query, limit)` — find sources
- `web_extract(URLs, char_limit)` — read content
- `browser_exec(code)` — interactive web tasks, JS-heavy pages

**Output:** Research findings with sources, facts vs. assumptions labelled, confidence level.

### 3.2 Projects Requests

**Triggers:**
- "Create a task for X"
- "What's the status of project Y?"
- "Update task Z to done"
- "Show me the roadmap"
- "Add a subtask"
- "Prioritise these tasks"
- "Track deadline for X"

**Routing:** Hermes invokes the ClickUp API wrapper script.

**Output:** Task created/updated/searched; task IDs; status summary.

### 3.3 Coding Requests

**Triggers:**
- "Write code for X"
- "Review this code"
- "Debug this error"
- "Explain this code"
- "Refactor/port this code"
- "Generate a script"
- "What's the best library for X?"
- "Create a repository for X"
- "Push this code to GitHub"
- "Create a PR"

**Routing:** Hermes delegates to Gemini CLI/API for code tasks, then uses GitHub CLI + git for publishing.

**Output:** Code (files/patches), analysis (reviews/explanations), debug diagnoses, PRs/issues.

### 3.4 General Requests (Hermes Direct)

**Triggers:**
- "Explain X concept"
- "Help me plan this"
- "What should I do about Y?"
- "Draft an email/message"
- "Summarise this for me"
- "Give me a recommendation on Z"

**Routing:** Hermes uses its own model (Solar Pro4 via Nous Portal) — no delegation.

**Output:** Direct response from Hermes.

### 3.5 Multi-Domain Requests

**Triggers:**
- "Analyse the current project status, identify outstanding technical work, research a technical issue, and prepare a client-ready status update" (the end-to-end test scenario)
- "Research X, create tasks for the findings, and write code for the top priority"

**Routing:** Hermes routes to multiple specialists in sequence:
1. Research (gather information)
2. Projects (create tasks from findings)
3. Coding (implement top priority)

Then synthesises the results into a coherent response.

### 3.6 Approval-Required Requests

**Triggers:**
- "Send a message to the client"
- "Change the scope of the project"
- "Push code to the main branch"
- "Create an invoice"
- "Delete a resource"
- "Change the deadline"

**Routing:** Hermes drafts the action, presents it to JUMA with DRAFT → HUMAN REVIEW → APPROVE → EXECUTE, waits for approval, then executes.

---

## 4. Handling Ambiguous Requests

When Hermes cannot confidently classify a request:

1. **Identify the ambiguity** — what is unclear? What are the possible interpretations?
2. **Make safe assumptions only if they are low-risk** — label them explicitly.
3. **Ask JUMA for clarification** — present the ambiguity and the options.
4. **Do not guess** — especially for consequential actions.

Example:
```
JUMA: "Handle the project"
Hermes: "I can help with the project, but I need to know what 'handle' means.
         Options:
         1. Create tasks from the project brief
         2. Update existing tasks
         3. Generate a status report
         4. Something else — please clarify
         Which would you like?"
```

---

## 5. Human-in-the-Loop Policy

For consequential actions, Hermes follows:

```
DRAFT → HUMAN REVIEW → APPROVE → EXECUTE
```

**Consequential actions requiring approval:**
- Sending client messages or emails
- Changing project scope
- Committing/pushing code (especially to main/production branches)
- Creating invoices
- Modifying production infrastructure
- Deleting resources
- Changing deadlines
- Any action that cannot be easily reversed

**Non-consequential actions (no approval needed):**
- Creating tasks in ClickUp (draft status)
- Creating branches in git
- Running read-only queries
- Generating drafts for JUMA to review
- Internal tool invocations that don't affect external state

JUMA can override this policy for specific actions by explicitly authorising autonomous execution.

---

## 6. Failure Handling

When a specialist tool fails:

1. **Detect** — Hermes notices the failure (error response, timeout, empty output).
2. **Diagnose** — Try to understand why (auth failure, rate limit, network error, invalid input).
3. **Retry once** — With corrected input or after a short backoff.
4. **Alternative approach** — If the first tool failed, try an alternative (e.g. Gemini API instead of CLI; curl instead of wrapper script).
5. **Report to JUMA** — If recovery fails, report the failure transparently with the reason and what was tried.

**Never enter infinite retry loops.** Use bounded retries (max 1-2 retries per tool invocation).

---

## 7. State Management

- **ClickUp** is the source of truth for project/task state.
- **GitHub** is the source of truth for code state.
- **OpenClaw session memory** provides some conversation state persistence.
- **Files** (prompts, scripts, artefacts) are stored in the project directory.
- Hermes records relevant IDs (task IDs, commit SHAs, file paths) for traceability.

---

## 8. Routing Decision Flow (Summary)

```
                        ┌─────────────────────────────┐
                        │        JUMA'S REQUEST        │
                        └─────────────┬───────────────┘
                                      │
                        ┌─────────────▼───────────────┐
                        │   HERMES: UNDERSTAND +      │
                        │   CLASSIFY THE REQUEST      │
                        └─────────────┬───────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                         │
  ┌────────▼────────┐    ┌───────────▼───────────┐   ┌───────▼────────┐
  │  RESEARCH need  │    │  PROJECTS need        │   │  CODING need   │
  │  (external knowl-│   │  (tasks, deadlines,   │   │  (code, repos,  │
  │  -edge, sources, │   │   clients, planning)  │   │  debug, review, │
  │   competition)   │   │                        │   │  architecture)  │
  └────────┬────────┘    └───────────┬───────────┘   └───────┬────────┘
           │                          │                         │
           ▼                          ▼                         ▼
  ┌───────────────┐      ┌─────────────────┐      ┌──────────────────┐
  │ web_search +  │      │ ClickUp API     │      │ Gemini CLI/API   │
  │ web_extract + │      │ (wrapper script)│      │ → gh + git for   │
  │ browser_exec  │      │                 │      │ publishing code  │
  └───────────────┘      └─────────────────┘      └──────────────────┘
           │                          │                         │
           ▼                          ▼                         ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                     HERMES SYNTHESIS + VALIDATION                    │
  └─────────────────────────────────────────────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                   RETURN TO JUMA (or request approval)              │
  └─────────────────────────────────────────────────────────────────────┘
```

---

*Phase 3 complete. Moving to Phase 4 — Specialist Agent Definitions.*
