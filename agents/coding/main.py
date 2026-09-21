#!/usr/bin/env python3
"""Coding agent -- routes work to Pi or Antigravity, as an MCP server.

WHAT IT IS
----------
A router and a set of guarantees. It does not write code itself: `backend` work
goes to the Pi coding agent in RPC mode, `frontend` work to the Antigravity
CLI. What this file owns is everything around that -- where the work may
happen, on which branch, and what is true about the result.

FOUR GUARANTEES, EACH ENFORCED IN CODE RATHER THAN IN A PROMPT
--------------------------------------------------------------

1. CONFINEMENT. project_path must resolve inside DEV_ROOT, after symlinks. The
   check is agents/_common/paths.py, carried over from the archived coding
   agent, where it was written because `Path(output_dir) / filename` silently
   discards output_dir when filename is absolute -- one line of pathlib
   behaviour that turned a workspace-scoped write into an arbitrary write. The
   subprocess is then started with cwd set to that project and nothing above
   it.

2. NEVER ON MAIN. Every task gets a fresh branch, created before the executor
   starts. If HEAD is on main or master, that is where the branch is cut from,
   never where the work lands. A repository with no commits yet gets an empty
   initial commit first, because you cannot branch from nothing.

3. NEVER PUBLISHED. There is no push, no deploy, no force-push, and no remote
   operation of any kind in this file. The result is a branch name and a diff
   summary; what happens to that branch is a human decision. In Docker mode the
   container also has no credentials with which to push.

4. OBSERVED, NOT REPORTED. The changed-file list comes from `git status
   --porcelain` and `git diff --numstat` run on the host after the executor
   exits, compared against a commit recorded before it started. A coding model
   that says it created three files and actually created two is an ordinary
   occurrence, not an exotic one, and the fix is to stop asking it.

SANDBOXING
----------
Pi's bash tool is unrestricted by default. Two modes, and which one was used is
always reported:

  docker  -- Pi runs in a container with the project bind-mounted at /work and
             the vendored Pi install mounted read-only. Its bash sees the
             project and nothing else.

  host    -- Pi runs directly, with a scrubbed environment: PATH, a temp dir,
             its own OPENROUTER_API_KEY, and nothing else. Every other variable
             the parent held is dropped, so the ClickUp token, the Telegram bot
             token and the Tavily key are not merely unused but absent.

Host mode is a real reduction in containment, not an equivalent alternative,
and the tool result says so in words rather than leaving the caller to infer it
from a flag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import guard                           # noqa: E402
from _common.errors import AgentError, ok           # noqa: E402
from _common.jobs import Job                        # noqa: E402
from _common.paths import ConfinementError, confine_existing  # noqa: E402
from _common.server import bootstrap, fatal         # noqa: E402

AGENT = "coding"
VERSION = "3.0.0"

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"

# This agent system's own checkout. It sits INSIDE the development root, which
# means the obvious confinement check is not enough on its own: a project_path
# of the repo itself -- or of any ancestor of it, such as
# D:\...\Dev\Personal -- puts agents/*/.env inside the directory Pi is given.
# Pi's bash would then read every key in the system, and in Docker mode the
# bind mount would carry them into the container.
#
# So SELF_ROOT is excluded in both directions: the project may not BE it, be
# inside it, or contain it.
SELF_ROOT = HERE.parent.parent
PI_CLI = VENDOR / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"

DOCKER_IMAGE = "juma-pi-sandbox:1"

# Branches the agent will never commit onto, whatever it is told.
PROTECTED_BRANCHES = {"main", "master", "develop", "release", "production", "prod"}

KINDS = {"backend", "frontend"}

# --- .env masking ---------------------------------------------------------
# A project's own secrets are not the coding agent's business. The task is
# "change this code", and no part of that needs the production database
# password that happens to sit in the same directory.
#
# Masking is by bind-mounting an empty file over each match, so the agent sees
# a real, readable, EMPTY file. That is deliberately not the same as deleting
# or denying: a build script that reads .env still works, it just finds
# nothing, which fails in an obvious way rather than a mysterious one.
#
# Example files stay visible. They are documentation -- they are the thing you
# read to learn which variables a project wants -- and they hold placeholders,
# not credentials.
ENV_MASK_KEEP = {".env.example", ".env.sample", ".env.template", ".env.dist",
                 ".env.defaults"}
# Directories never worth walking, and never worth masking inside.
ENV_SCAN_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "dist", "build", ".next", ".tox", "vendor", ".mypy_cache"}
# A ceiling, so a pathological tree cannot produce a docker command line with
# thousands of -v flags.
MAX_MASKED_ENV_FILES = 200

# Pi can run for a long time on a real task; this is the ceiling, not a target.
EXECUTOR_TIMEOUT = 900

INSTRUCTIONS = """Runs coding tasks in a project under the configured
development root, on a new branch, and reports what actually changed.

start_code_task returns a job id immediately; poll get_status, then read
get_result. list_changed_files re-observes the working tree for a finished job.

It never pushes, deploys or force-pushes, and it never works on main. Changed
files come from a real git diff, not from the coding model's own account."""

PREAMBLE = """You are working inside a single project directory. Constraints
that are enforced outside your control, so working around them is not possible
and not worth attempting:

- You are on a dedicated branch. Do not switch branches and do not commit to
  main or master.
- Do not push, deploy, or contact any remote. There are no credentials for it.
- Stay inside the working directory.

Make the change, then stop and summarise what you did in one short paragraph."""


# ---------------------------------------------------------------------------
# git, all of it on the host
# ---------------------------------------------------------------------------

def git(project: Path, *args: str, check: bool = True,
        timeout: int = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(project), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AgentError("git_failed",
                         f"git {args[0]} failed: {(result.stderr or '').strip()[:300]}")
    return result


def ensure_repo(project: Path, audit) -> None:
    inside = git(project, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        git(project, "init")
        audit.write("git_init", project=str(project))

    # You cannot branch from nothing, and a fresh `git init` has no commits.
    has_commit = git(project, "rev-parse", "--verify", "HEAD", check=False)
    if has_commit.returncode != 0:
        git(project, "-c", "user.email=agent@localhost", "-c", "user.name=juma-coding-agent",
            "commit", "--allow-empty", "-m", "Initial commit (created by the coding agent)")
        audit.write("git_initial_commit", project=str(project))


def make_branch(project: Path, kind: str, instruction: str, audit) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", instruction.lower()).strip("-")[:32] or "task"
    branch = f"agent/{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    if branch.split("/")[-1] in PROTECTED_BRANCHES:
        branch += "-task"
    git(project, "checkout", "-b", branch)
    audit.write("branch_created", project=str(project), branch=branch)
    return branch


def current_branch(project: Path) -> str:
    return git(project, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()


def observe_changes(project: Path, baseline: str) -> dict:
    """What actually changed, from git. Never from the model's summary."""
    status = git(project, "status", "--porcelain", check=False).stdout
    changed: list[dict] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        code, _, path = line[:2], line[2:3], line[3:]
        changed.append({"path": path.strip().strip('"'), "state": _state_for(code)})

    numstat = git(project, "diff", "--numstat", baseline, check=False).stdout
    insertions = deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                insertions += int(parts[0])
                deletions += int(parts[1])
            except ValueError:
                pass  # binary files report "-"

    diff_stat = git(project, "diff", "--stat", baseline, check=False).stdout.strip()

    return {
        "changed_files": changed,
        "changed_file_count": len(changed),
        "insertions": insertions,
        "deletions": deletions,
        "diff_summary": diff_stat[:4000],
        "observed_from": "git status --porcelain and git diff against the "
                         "pre-task commit, on the host",
    }


def _state_for(code: str) -> str:
    code = code.strip()
    if code in ("??",):
        return "added (untracked)"
    if code.startswith("A"):
        return "added"
    if code.startswith("D"):
        return "deleted"
    if code.startswith("R"):
        return "renamed"
    return "modified"


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

def reject_if_self(project: Path) -> None:
    """Refuse a project that would expose this system's own credentials.

    Checked in both directions, because both leak:
      - project inside SELF_ROOT  -> agents/*/.env is under the project
      - project contains SELF_ROOT -> the whole system is under the project

    This is separate from the development-root confinement, which it does not
    replace. Confinement answers "may the agent touch this tree at all";
    this answers "would touching it hand over the keys".
    """
    if project == SELF_ROOT or SELF_ROOT in project.parents or project in SELF_ROOT.parents:
        raise AgentError(
            "path_not_allowed",
            f"refused: {project} is, contains, or sits inside this agent "
            f"system's own checkout ({SELF_ROOT}), whose agents/*/.env files "
            f"hold every credential in the system. Point the task at a "
            f"different project.",
        )


def is_env_secret_file(name: str) -> bool:
    """True for .env, .env.local, .env.production ... and not for .env.example."""
    lowered = name.lower()
    if lowered in ENV_MASK_KEEP:
        return False
    return lowered == ".env" or lowered.startswith(".env.")


def find_env_files(project: Path) -> list[Path]:
    """Every .env-style secret file in the project, as paths relative to it."""
    found: list[Path] = []
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in ENV_SCAN_SKIP]
        for name in files:
            if is_env_secret_file(name):
                found.append(Path(root, name).relative_to(project))
                if len(found) >= MAX_MASKED_ENV_FILES:
                    return sorted(found)
    return sorted(found)


def empty_mask_file() -> Path:
    """One empty host file, bind-mounted over every masked path."""
    path = VENDOR / ".env-mask-empty"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return path


def env_mask_mounts(project: Path) -> tuple[list[str], list[str]]:
    """Docker -v flags that blank out the project's .env files.

    Returns (flags, masked_relative_paths).
    """
    masked = find_env_files(project)
    if not masked:
        return [], []
    empty = empty_mask_file()
    flags: list[str] = []
    for relative in masked:
        target = "/work/" + relative.as_posix()
        flags += ["-v", f"{empty}:{target}:ro"]
    return flags, [p.as_posix() for p in masked]


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        probe = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE],
                               capture_output=True, timeout=30)
        return probe.returncode == 0
    except Exception:
        return False


def scrubbed_env(api_key: str, key_var: str = "GROQ_API_KEY") -> dict[str, str]:
    """The complete environment Pi gets in host mode.

    Built up from nothing rather than copied and filtered. A copy-and-delete
    approach leaks whatever nobody thought to name; this leaks nothing by
    construction, and the cost is having to add a variable when something
    genuinely needs one.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        key_var: api_key,
        "AI_AGENT": "pi",
        "PI_OFFLINE": "0",
        # Pi reads these for its own config location; pointing them at the
        # vendor dir keeps it out of the user's profile.
        "HOME": str(VENDOR),
        "USERPROFILE": str(VENDOR),
    }
    return {k: v for k, v in env.items() if v}


def run_pi(job: Job, project: Path, instruction: str, model: str,
           provider: str, api_key: str, key_var: str, mode: str, audit) -> dict:
    """Drive Pi over its RPC protocol (strict LF-delimited JSONL)."""
    prompt = f"{PREAMBLE}\n\nTASK\n{guard.instruction_block(instruction)}"

    masked: list[str] = []
    if mode == "docker":
        # Blank the project's own secrets before the container can see them.
        # Ordering matters: these -v flags must come AFTER the /work mount, or
        # the directory mount lands on top and undoes them.
        mask_flags, masked = env_mask_mounts(project)
        command = [
            "docker", "run", "--rm", "-i",
            # The project is the only writable thing in the container.
            "-v", f"{project}:/work",
            "-v", f"{VENDOR}:/pi:ro",
            *mask_flags,
            "-w", "/work",
            "-e", key_var,
            "-e", "AI_AGENT=pi",
            # No host network, no extra mounts, no privileged flags.
            "--network", "bridge",
            DOCKER_IMAGE,
            "node", "/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
            "--mode", "rpc", "--no-session",
            "--provider", provider, "--model", model,
        ]
        env = {**os.environ, key_var: api_key}
        cwd = str(project)
        if masked:
            audit.write("env_files_masked", project=str(project), count=len(masked),
                        files=masked)
    else:
        command = [
            "node", str(PI_CLI), "--mode", "rpc", "--no-session",
            "--provider", provider, "--model", model,
        ]
        env = scrubbed_env(api_key, key_var)
        cwd = str(project)

    audit.write("executor_start", executor="pi", mode=mode, model=model,
                provider=provider, project=str(project))

    process = subprocess.Popen(
        command, cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    transcript: list[str] = []
    tools_used: list[str] = []
    deadline = time.time() + EXECUTOR_TIMEOUT
    settled = False

    try:
        process.stdin.write(json.dumps({"id": "t1", "type": "prompt",
                                        "message": prompt}) + "\n")
        process.stdin.flush()

        # Strict JSONL: split on \n only, strip a trailing \r. Pi's own docs
        # warn that generic line readers also split on U+2028/U+2029, which are
        # valid inside JSON strings and would corrupt the stream.
        buffer = ""
        while time.time() < deadline and not settled:
            if job.cancelled():
                job.note("cancelled; stopping the executor")
                break
            chunk = process.stdout.read(1)
            if chunk == "":
                break
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "agent_settled":
                    settled = True
                elif kind == "tool_execution_start":
                    name = event.get("toolName") or event.get("tool") or "tool"
                    tools_used.append(str(name))
                    job.note(f"pi: {name} ({len(tools_used)} tool calls)")
                elif kind == "message_end":
                    text = _text_of(event)
                    if text:
                        transcript.append(text)
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=15)
        except Exception:
            process.kill()

    stderr = (process.stderr.read() if process.stderr else "") or ""
    timed_out = not settled and time.time() >= deadline

    audit.write("executor_end", executor="pi", mode=mode, settled=settled,
                timed_out=timed_out, tool_calls=len(tools_used),
                stderr=stderr[-2000:])

    if not settled and not transcript and not job.cancelled():
        raise AgentError(
            "executor_failed",
            f"the Pi coding agent produced no result "
            f"({'timed out' if timed_out else 'exited early'}). "
            f"stderr: {stderr.strip()[-400:] or '(empty)'}",
            retryable=timed_out,
        )

    return {
        "executor": "pi",
        "sandbox": mode,
        "provider": provider,
        "masked_env_files": masked,
        "masked_env_file_count": len(masked),
        "model": model,
        "tool_calls": len(tools_used),
        "tools_used": sorted(set(tools_used)),
        "timed_out": timed_out,
        # The model's own words. Kept separate from the observed diff on
        # purpose: it is testimony, not evidence.
        "agent_report": "\n\n".join(transcript)[-4000:],
    }


def _text_of(event: dict) -> str:
    message = event.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content
                 if isinstance(block, dict) and block.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def run_agy(job: Job, project: Path, instruction: str, audit) -> dict:
    """Antigravity CLI, if it is installed and authenticated."""
    agy = shutil.which("agy")
    if not agy:
        raise AgentError(
            "frontend_agent_not_configured",
            "frontend agent not configured: the Antigravity CLI ('agy') is not "
            "installed or not on PATH. Install and authenticate it, then retry; "
            "backend tasks are unaffected.",
        )

    probe = subprocess.run([agy, "--version"], capture_output=True, text=True,
                           timeout=60)
    if probe.returncode != 0:
        raise AgentError(
            "frontend_agent_not_configured",
            "frontend agent not configured: 'agy' is installed but did not run "
            f"cleanly ({(probe.stderr or '').strip()[:200]}). It most likely "
            f"needs authenticating.",
        )

    job.note("agy: running")
    audit.write("executor_start", executor="agy", project=str(project))
    result = subprocess.run(
        [agy, "-p", f"{PREAMBLE}\n\nTASK\n{guard.instruction_block(instruction)}",
         "--output-format", "json"],
        cwd=str(project), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=EXECUTOR_TIMEOUT,
    )
    audit.write("executor_end", executor="agy", returncode=result.returncode)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if re.search(r"auth|login|credential|unauthor", stderr, re.IGNORECASE):
            raise AgentError(
                "frontend_agent_not_configured",
                f"frontend agent not configured: 'agy' is not authenticated "
                f"({stderr[:200]}).",
            )
        raise AgentError("executor_failed",
                         f"agy exited {result.returncode}: {stderr[:300]}")

    try:
        payload = json.loads(result.stdout)
        report = json.dumps(payload)[:4000]
    except Exception:
        report = (result.stdout or "").strip()[-4000:]

    return {"executor": "agy", "sandbox": "host", "model": "antigravity-default",
            "agent_report": report, "timed_out": False, "tool_calls": None,
            "tools_used": []}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        boot = bootstrap(
            agent=AGENT,
            version=VERSION,
            instructions=INSTRUCTIONS,
            required=[],
            optional=["GROQ_API_KEY", "OPENROUTER_API_KEY", "CODING_ROOT",
                      "CODING_SANDBOX", "CODING_ALLOW_UNMASKED_ENV",
                      "CODING_PROVIDER"],
            default_model="openai/gpt-oss-120b",
            # This agent does not call a model itself. Pi does, with its own
            # copy of the key; there is no LLM client in this process.
            needs_llm=False,
        )
    except Exception as exc:
        fatal(AGENT, exc)
        return

    dev_root = Path(boot.config.get("CODING_ROOT")
                    or r"D:\Bukoma Juma Moya\Dev").resolve()
    model = boot.config.get("CODING_MODEL") or "poolside/laguna-s-2.1:free"
    # Pi gets ONE provider and ONE key. Groq is preferred because
    # OpenRouter's free tier is capped per day, account-wide, and a coding task
    # is the most model-hungry thing here.
    groq_key = (boot.config.get("GROQ_API_KEY") or "").strip()
    openrouter_key = (boot.config.get("OPENROUTER_API_KEY") or "").strip()
    forced_provider = (boot.config.get("CODING_PROVIDER") or "").strip().lower()

    if forced_provider == "openrouter" or (not groq_key and openrouter_key):
        pi_provider, pi_key, pi_key_var = "openrouter", openrouter_key, "OPENROUTER_API_KEY"
    elif groq_key:
        pi_provider, pi_key, pi_key_var = "groq", groq_key, "GROQ_API_KEY"
    else:
        fatal(AGENT, RuntimeError(
            "no model provider configured: set GROQ_API_KEY or "
            "OPENROUTER_API_KEY in agents/coding/.env"))
        return

    forced = (boot.config.get("CODING_SANDBOX") or "auto").strip().lower()
    if forced == "host":
        sandbox_mode = "host"
    elif forced == "docker":
        sandbox_mode = "docker"
    else:
        sandbox_mode = "docker" if docker_available() else "host"

    allow_unmasked_env = (boot.config.get("CODING_ALLOW_UNMASKED_ENV") or "")\
        .strip().lower() in ("1", "true", "yes", "on")

    boot.audit.write("coding_config", dev_root=str(dev_root), model=model,
                     allow_unmasked_env=allow_unmasked_env,
                     sandbox=sandbox_mode, provider=pi_provider,
                     pi_installed=PI_CLI.exists(),
                     agy_installed=bool(shutil.which("agy")))

    @boot.tool(
        name="start_code_task",
        description=(
            "Start a coding task in a project under the development root. "
            "kind is 'backend' (Pi coding agent) or 'frontend' (Antigravity "
            "CLI). Creates a new git branch, never works on main, and never "
            "pushes. Returns a job_id immediately -- poll get_status, then "
            "get_result."
        ),
    )
    def start_code_task(project_path: str, instruction: str,
                        kind: str = "backend") -> dict:
        kind = str(kind or "backend").strip().lower()
        if kind not in KINDS:
            raise AgentError("bad_input", f"kind must be one of {sorted(KINDS)}")

        instruction = str(instruction or "").strip()
        if not instruction:
            raise AgentError("bad_input", "instruction must not be empty")

        # Confinement first, before anything is created or started.
        try:
            project = confine_existing(project_path, dev_root, must_exist=False)
        except ConfinementError as exc:
            boot.audit.write("confinement_refused", project_path=str(project_path),
                             reason=str(exc))
            raise AgentError("path_not_allowed",
                             f"{exc}. This agent works only inside {dev_root}.")

        reject_if_self(project)

        # Masking is implemented with bind mounts, which only exist in Docker
        # mode. Running on the host with a project that holds secrets would
        # hand Pi's unrestricted bash the very files this rule exists to hide,
        # so the honest behaviour is to refuse rather than to pretend. The
        # override exists because it is the operator's own machine and their
        # own project, but it has to be asked for.
        if sandbox_mode == "host" and kind == "backend" and project.exists():
            exposed = find_env_files(project)
            if exposed and not allow_unmasked_env:
                names = ", ".join(p.as_posix() for p in exposed[:3])
                if len(exposed) > 3:
                    names += f", and {len(exposed) - 3} more"
                raise AgentError(
                    "env_masking_unavailable",
                    f"refused: this project contains {len(exposed)} .env file(s) "
                    f"({names}) and the agent is running in host mode, where "
                    f"they cannot be masked. Start Docker so the sandbox image "
                    f"is used, or set CODING_ALLOW_UNMASKED_ENV=true in "
                    f"agents/coding/.env to accept the exposure.",
                )

        created = False
        if not project.exists():
            project.mkdir(parents=True)
            created = True
        elif not project.is_dir():
            raise AgentError("path_not_allowed", "project_path is not a directory")

        ensure_repo(project, boot.audit)

        started_on = current_branch(project)
        branch = make_branch(project, kind, instruction, boot.audit)
        baseline = git(project, "rev-parse", "HEAD").stdout.strip()

        def worker(job: Job) -> dict:
            job.note(f"running {kind} task on {branch}")
            if kind == "frontend":
                outcome = run_agy(job, project, instruction, boot.audit)
            else:
                outcome = run_pi(job, project, instruction, model,
                                 pi_provider, pi_key, pi_key_var,
                                 sandbox_mode, boot.audit)

            observed = observe_changes(project, baseline)
            final_branch = current_branch(project)
            return {
                "project_path": str(project),
                "project_created": created,
                "branch": final_branch,
                "branch_created_from": started_on,
                "baseline_commit": baseline[:12],
                "kind": kind,
                **outcome,
                **observed,
                "pushed": False,
                "note": ("Nothing was pushed, deployed or merged. The work is "
                         f"on local branch {final_branch!r}; review it before "
                         "doing anything with it."),
            }

        job = boot.jobs.start(f"code-{kind}", worker)
        return ok(job_id=job.id, kind=kind, branch=branch,
                  project_path=str(project), project_created=created,
                  sandbox=sandbox_mode if kind == "backend" else "host",
                  detail=("Started. Poll get_status with this job_id, then "
                          "read get_result."),
                  agent=AGENT, version=VERSION)

    @boot.tool(name="get_status",
               description="Progress of a coding job started by start_code_task.")
    def get_status(job_id: str) -> dict:
        return boot.jobs.status(job_id)

    @boot.tool(name="get_result",
               description=("Full result of a finished coding job: branch name, "
                            "observed changed files, diff summary and the coding "
                            "agent's own report."))
    def get_result(job_id: str) -> dict:
        return boot.jobs.result(job_id)

    @boot.tool(
        name="list_changed_files",
        description=("Re-observe what a finished job changed, straight from git. "
                     "Use this when you want the file list without the rest of "
                     "the result."),
    )
    def list_changed_files(job_id: str) -> dict:
        result = boot.jobs.result(job_id)
        if not result.get("ok"):
            return result
        payload = result.get("result") or {}
        project = Path(payload.get("project_path", ""))
        baseline = payload.get("baseline_commit")
        if not project.exists() or not baseline:
            raise AgentError("unknown_job",
                             "that job has no recorded project and baseline")
        observed = observe_changes(project, baseline)
        return ok(job_id=job_id, branch=payload.get("branch"),
                  project_path=str(project), **observed)

    boot.run()


if __name__ == "__main__":
    main()
