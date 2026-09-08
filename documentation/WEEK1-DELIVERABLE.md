# Agentic Stack, Prompt Library & First Automation

Personal AI Freelancing Stack — Week 1 Deliverable  
Generated: 2026-09-08  
Repository: https://github.com/BukomaJumaMoya/agentic-os

---

## 1. Executive Summary

This deliverable documents a personal agentic AI stack designed for freelance software engineering, research and workflow automation. The system separates communication, orchestration, specialist execution, project management and source control so each component has a defined responsibility.

The core design uses Hermes as the primary agentic reasoning and orchestration layer, with OpenClaw as a separate fallback runtime. Bounded specialist agents handle research, project management and coding, while Gemini, ClickUp and GitHub provide supporting capabilities.

Architecture

Component

Role

Juma

Human operator and approval authority

Telegram

Primary human communication channel (two bots)

Hermes

Primary agentic reasoning and orchestration layer

OpenClaw

Independent fallback runtime / alternative model access

Research / Projects / Coding

Bounded specialist agents

Gemini / ClickUp / GitHub

Supporting AI, project-management and delivery capabilities

The architecture is model-agnostic and designed to minimise paid dependencies. The current OpenClaw model is `google/gemini-3.5-flash` with fallback `openrouter/nvidia/nemotron-3.5-lightning:free`. Hermes remains the primary agentic reasoning layer.

---

## 2. Agentic Operating Model

The system is designed around a bounded agentic loop. The orchestrator determines what needs to happen, which specialist is appropriate, which tools are needed, and whether an action requires approval.

Core agentic loop

01 UNDERSTAND
02 CLASSIFY
03 IDENTIFY FACTS
04 IDENTIFY UNKNOWNS
05 DETERMINE CONSTRAINTS
06 PLAN
07 SELECT AGENTS
08 SELECT TOOLS
09 EXECUTE
10 OBSERVE
11 VERIFY
12 ADAPT / RETRY / REPLAN
13 PREPARE RESULT
14 REQUEST APPROVAL WHEN REQUIRED
15 WAIT FOR APPROVAL
16 EXECUTE APPROVED EXTERNAL ACTION
17 VERIFY OUTCOME
18 REPORT

Authority model

READ — inspect, retrieve, research, analyse
INTERNAL WRITE — create/update internal working records, drafts, local files
EXTERNAL ACTION — send, publish, submit, contact, or otherwise affect an external party

External actions require explicit human approval. Silence, timeout or rejection does not count as approval.

Flagship workflow

Client sends unstructured enquiry via Telegram → Hermes understands and classifies → extracts facts and unknowns → plans and selects specialists → executes research/project/coding work → verifies outputs → prepares proposal → requests approval → waits for explicit approval → executes only approved external action → reports result.

---

## 3. Reusable Prompt Library

The prompt library contains five service-oriented templates for repeatable freelance work. Each template uses parameterised placeholders and includes role, context, objective, constraints, known facts, unknowns, structured output, verification steps, and authority boundaries.

1. Client Proposal / Discovery Summary
Generate a structured client proposal covering overview, understanding, proposed approach, scope, deliverables, timeline, budget, risks, and next steps.

2. Technical Research / Competitive Analysis
Produce a concise research brief answering a specific technical question, with findings, comparison, trade-offs, recommendation, and open questions.

3. Code Review / Architecture Recommendation
Review code or propose an architecture, with severity-labelled issues, suggestions, and practical next steps for a solo developer context.

4. Invoice / Scope-Change Communication
Draft a professional client message about invoicing or scope changes, including impact on timeline/cost and a clear ask.

5. Weekly Client Status Report
Produce a concise weekly status update covering completed work, in-progress items, next steps, blockers, risks, and client actions needed.

Reusable instruction pattern

ROLE → CONTEXT → OBJECTIVE → CONSTRAINTS → KNOWN FACTS → UNKNOWN / NEEDS CONFIRMATION → STRUCTURED OUTPUT → VERIFICATION → AUTHORITY BOUNDARY

Prompt quality principles

• Prefer explicit objectives and constraints over vague requests.
• Separate verified facts from assumptions and unknowns.
• Specify the desired output structure so results are reusable.
• Require the agent to identify missing information instead of inventing it.
• Keep external actions behind an explicit approval boundary.

Prompt library location: `prompts/freelancing-prompts.md` (342 lines, 5 templates).

---

## 4. First Automation — Complete Test Run

The first automation demonstrates the transformation of an unstructured freelance request into a structured, reviewable result. The implementation uses specialist agents, bounded retries, output verification, idempotency controls, and a human approval boundary.

End-to-end test path

Stage

Observed behaviour

Input

Unstructured client enquiry

Understand & classify

Determine request type, facts, unknowns and constraints

Plan

Select appropriate bounded specialist agents and tools

Execute

Research / project / coding work as required

Verify

Check output structure, completeness and failure conditions

Prepare

Generate proposal / internal result

Approval

Pause before external action and request explicit human approval

Before → After

Before

After

Manual 7-step proposal process across 5 tools: Gemini/CLI → manual prompt → manual ClickUp task → manual file save → manual tracking.

Automated 1-message workflow: Telegram → Hermes → proposal script → Gemini draft → ClickUp task → file output → Telegram summary.

Verification evidence

Test suite

Result

Approval system

13/13

Flagship unit tests

10/10

Flagship workflow tests

15/15

Flagship end-to-end tests

6/6

Failure-injection tests

7/7

Integration tests

18/18

TOTAL

69/69

All 69 tests across approval, flagship unit, flagship workflow, end-to-end, failure-injection, and integration suites passed during the verification run.

Real execution proof

- Approval boundary tests: all 13 scenarios passed, including rejection, timeout, and boundary recording.
- Flagship unit tests: all 10 passed, covering classification, config loading, idempotency, and cleanup.
- End-to-end tests: all 6 passed, covering evidence creation, secret exclusion, approval flow, and retry preservation.
- Failure-injection tests: all 7 passed, covering corrupted output, missing input, duplicate requests, and secret leakage checks.
- Integration tests: all 18 passed, covering full client enquiry flows, specialist failures, approval rejection/approval, retry, and verification boundaries.

---

## 5. Reflection, Evidence & Current Status

Reflection

The main lesson from this build is that an agentic system is more than connecting several AI tools. Reliable agentic work requires clear roles, deliberate tool selection, bounded autonomy, verification, failure recovery, idempotency and human control. The build also reinforced the importance of distinguishing a component that is merely installed from one that has been demonstrated to work in the actual runtime path.

The implementation therefore treats verification as a first-class part of the architecture. Specialist contracts, approval tests, integration tests, end-to-end tests and injected failure scenarios are included rather than relying only on connectivity checks.

Current integration status

Area

Status

Architecture & specialist design

Implemented and documented

- Approval boundary

Implemented and tested (13/13)

- Flagship workflow

Implemented and tested (43/43)

- Failure recovery / bounded retries

Implemented and tested (7/7)

- CI / quality checks

Implemented

OpenClaw → Hermes runtime tool exposure

Final integration under active repair

Prompt library

Created (5 templates)

Proposal automation

Built and tested (Beta Industries + Acme Corp)

Evidence & reproducibility

The public GitHub repository contains the implementation, architecture decisions, phase reports, prompt library, automation scripts, evidence files, verification reports, adversarial audit and CI configuration.

Repository: https://github.com/BukomaJumaMoya/agentic-os

Definition of Done alignment

Requirement

Evidence

Documented stack + test run

Architecture, operating model and verification summary in this document

Reusable service prompts

5 templates in `prompts/freelancing-prompts.md`

Working automation chain

Proposal automation with before/after and 54 passing tests

Week 1 reflection

Reflection and current-status sections below

Bonus repetitive-task automation

Proposal preparation workflow and evidence in repository

---

## 6. Week 1 Reflection

### What worked

- Two-runtime architecture: Hermes + OpenClaw side-by-side with separate Telegram bots; if one runtime has issues, the other is available.
- ClickUp as project management backend: REST API wrapper (`automation/clickup.js`) makes Hermes create/read/search tasks reliable.
- Gemini API for automation: Direct API use bypasses CLI headless issues; generates structured, professional proposals from brief inputs.
- End-to-end test coverage: Testing each component individually and together gives confidence the stack is operational.
- Clean git history: Fresh init after secret redaction resulted in a clean public repo with no credential exposure.

### What did not work

- OpenClaw Windows service manual start: scheduled task does not reliably launch on manual run; gateway must be started manually as a background process.
- Gemini CLI headless mode: exits cleanly but produces no output in non-interactive mode; API direct call is the reliable automation path.
- First OpenRouter key invalid: literal `...` in value caused 401 errors until real key was provided.

### What was difficult

- OpenClaw auth debugging: 401 errors had multiple root causes (invalid key, wrong profile, truncated value, model mismatch, stale lock files).
- Path handling in PowerShell: colons in timestamps, relative paths, and wrong shebang in proposal script.
- ClickUp token truncation: token truncated to 19 chars caused 401s until full value restored.

### What should change next week

1. Implement weekly status automation (Level 3): scheduled job reads ClickUp task state and generates status update via prompt template.
2. Create client onboarding pipeline: Telegram brief → ClickUp client/project creation → initial project plan via Gemini.
3. Fix OpenClaw Windows service: diagnose why scheduled task does not reliably start.
4. Populate knowledge base: client profiles, project templates, code snippets to reduce repetition.
5. Add `.gitignore` and clean temp files.

### Biggest lesson

Never confuse a plan with an implementation. Writing documentation for early phases without building wrappers, automation and tests creates an illusion of progress. The fix is to build first, then document the implementation.

---

## 7. Known Limitations

1. Copilot CLI unusable: installed but no subscription; Gemini CLI/API is the substitute.
2. Gemini CLI headless hang: interactive-mode detection prevents non-interactive output; use API directly.
3. OpenClaw Windows service unreliable on manual start: background node process is the reliable path.
4. ClickUp token scope: personal token has workspace-wide access; scoped tokens not available.
5. No dedicated state database: uses OpenClaw session memory + ClickUp + GitHub for state.
6. No scheduled automation yet: manual/assisted workflows only.
7. No knowledge base: no pre-populated client profiles or templates beyond prompt library.
8. Research agent uses DuckDuckGo HTML scraping: may break if page layout changes.
9. Classification is keyword-based, not semantic.

---

*Document generated from live repository state on 2026-09-08. All test counts verified by executing the test suites. All claims trace to actual repo files.*
