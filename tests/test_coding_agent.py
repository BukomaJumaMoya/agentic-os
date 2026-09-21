#!/usr/bin/env python3
"""Coding agent: confinement, branch policy, and observed-not-reported changes.

Run: agents/.venv/Scripts/python tests/test_coding_agent.py
Add --live to actually run Pi against a scratch project (needs a working
OpenRouter key and costs a model call).

The offline tests are the security ones. They are deliberately the majority:
"it works" is easy to establish once and cheap to re-establish; "it cannot
escape" has to be re-established on every change.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "agents" / "coding"))

from mcp_client import MCPStdioClient  # noqa: E402

PY = str(REPO / "agents" / ".venv" / "Scripts" / "python.exe")
AGENT_PATH = str(REPO / "agents" / "coding" / "main.py")
DEV_ROOT = Path(r"D:\Bukoma Juma Moya\Dev")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


def test_no_publish_primitives() -> None:
    """No push, no deploy, no force-push anywhere in the source."""
    source = Path(AGENT_PATH).read_text(encoding="utf-8")
    code_only = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    # Strip docstrings so prose about not pushing does not trip the check.
    code_only = re.sub(r'""".*?"""', "", code_only, flags=re.DOTALL)

    for label, pattern in {
        "git push": r'"push"|\bgit\s+push\b',
        "force push": r'--force\b|-f\b.*push|\+refs/',
        "remote add": r'"remote"',
        "deploy": r'\bdeploy\b',
        "fetch/pull": r'"(?:fetch|pull|clone)"',
    }.items():
        hits = re.findall(pattern, code_only, re.IGNORECASE)
        check(f"source contains no {label}", not hits, f"found {hits[:3]}")


def test_protected_branches() -> None:
    import main as coding  # noqa: PLC0415

    for name in ("main", "master", "develop", "production"):
        check(f"{name} is a protected branch", name in coding.PROTECTED_BRANCHES)


def test_branch_is_created_and_never_main() -> None:
    import main as coding  # noqa: PLC0415

    class FakeAudit:
        def write(self, *_args, **_kwargs) -> None:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "scratch"
        project.mkdir()
        coding.ensure_repo(project, FakeAudit())

        before = coding.current_branch(project)
        branch = coding.make_branch(project, "backend", "add a hello script", FakeAudit())
        after = coding.current_branch(project)

        check("a branch is created", branch == after, f"{branch!r} vs {after!r}")
        check("the branch is not main/master",
              after not in coding.PROTECTED_BRANCHES, after)
        check("the branch is namespaced under agent/", after.startswith("agent/"), after)
        check("the branch differs from the starting branch", after != before,
              f"{before} -> {after}")

        # An empty repo must still be branchable.
        check("an empty repo got an initial commit",
              coding.git(project, "rev-parse", "--verify", "HEAD",
                         check=False).returncode == 0)


def test_changes_are_observed_from_git() -> None:
    import main as coding  # noqa: PLC0415

    class FakeAudit:
        def write(self, *_args, **_kwargs) -> None:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "scratch"
        project.mkdir()
        coding.ensure_repo(project, FakeAudit())
        coding.make_branch(project, "backend", "test", FakeAudit())
        baseline = coding.git(project, "rev-parse", "HEAD").stdout.strip()

        (project / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (project / "notes.md").write_text("# notes\n", encoding="utf-8")

        observed = coding.observe_changes(project, baseline)
        paths = sorted(f["path"] for f in observed["changed_files"])

        check("observed changes list the real files",
              paths == ["hello.py", "notes.md"], str(paths))
        check("observed change count is right",
              observed["changed_file_count"] == 2, str(observed))
        check("observation says where it came from",
              "git status" in observed["observed_from"])

        # The key property: a file the model never mentions still shows up,
        # and a file it invents does not.
        check("observation is independent of any model report",
              "hello.py" in paths and "imaginary.py" not in paths)


def test_confinement_offline() -> None:
    from _common.paths import ConfinementError, confine_existing  # noqa: PLC0415

    outside = [
        r"C:\Windows\System32",
        r"C:\Users\HP\AppData\Local\hermes",
        str(REPO.parent.parent.parent),
        "..",
        r"D:\Bukoma Juma Moya",
        "/etc",
    ]
    for candidate in outside:
        try:
            confine_existing(candidate, DEV_ROOT, must_exist=False)
            check(f"rejects {candidate!r}", False, "it was allowed")
        except ConfinementError:
            check(f"rejects {candidate!r}", True)

    inside = confine_existing(str(DEV_ROOT / "Personal"), DEV_ROOT, must_exist=False)
    check("allows a path inside the dev root", str(inside).startswith(str(DEV_ROOT)))


def test_scrubbed_env_is_built_from_nothing() -> None:
    import os  # noqa: PLC0415

    import main as coding  # noqa: PLC0415

    os.environ["CLICKUP_TOKEN"] = "pk_1_SHOULDNOTREACHPI"
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:SHOULDNOTREACHPI"
    os.environ["TAVILY_API_KEY"] = "tvly-SHOULDNOTREACHPI"

    # Groq is the default provider now; the key variable is a parameter so the
    # same scrub works whichever limb of the chain Pi is pointed at.
    env = coding.scrubbed_env("gsk_thekey", "GROQ_API_KEY")
    check("Pi receives its own Groq key",
          env.get("GROQ_API_KEY") == "gsk_thekey", str(sorted(env)))
    check("Pi receives no OpenRouter key when running on Groq",
          "OPENROUTER_API_KEY" not in env, str(sorted(env)))

    or_env = coding.scrubbed_env("sk-or-v1-thekey", "OPENROUTER_API_KEY")
    check("the OpenRouter limb passes its own key instead",
          or_env.get("OPENROUTER_API_KEY") == "sk-or-v1-thekey"
          and "GROQ_API_KEY" not in or_env, str(sorted(or_env)))

    for name in ("CLICKUP_TOKEN", "TELEGRAM_BOT_TOKEN", "TAVILY_API_KEY"):
        check(f"Pi does not receive {name}", name not in env, f"{name} leaked")
    check("no value in Pi's environment contains another agent's secret",
          not any("SHOULDNOTREACHPI" in v for v in env.values()),
          str([k for k, v in env.items() if "SHOULDNOTREACHPI" in v]))
    check("Pi's environment is small and enumerable", len(env) <= 12, str(sorted(env)))


def test_cannot_be_pointed_at_its_own_secrets() -> None:
    """The agent system's own checkout sits inside the development root.

    Without the SELF_ROOT rule, a project_path of this repo -- or of any
    ancestor of it -- would put agents/*/.env inside the directory Pi gets,
    handing Pi's unrestricted bash every credential in the system.
    """
    import main as coding  # noqa: PLC0415

    hostile = [
        ("the repo itself", REPO),
        ("a directory inside the repo", REPO / "agents"),
        ("the agent holding the keys", REPO / "agents" / "pm"),
        ("an ancestor containing the repo", REPO.parent),
        ("the development root itself", DEV_ROOT),
    ]
    for label, path in hostile:
        try:
            coding.reject_if_self(Path(path).resolve())
            check(f"refuses {label}", False, f"{path} was allowed")
        except coding.AgentError as exc:
            check(f"refuses {label}", exc.code == "path_not_allowed", exc.code)

    # An unrelated project under the dev root stays allowed.
    try:
        coding.reject_if_self((DEV_ROOT / "scratch" / "some-project").resolve())
        check("still allows an unrelated project", True)
    except coding.AgentError as exc:
        check("still allows an unrelated project", False, exc.detail)


def test_server_rejects_its_own_checkout() -> None:
    with MCPStdioClient([PY, AGENT_PATH]) as client:
        for path in (str(REPO), str(REPO / "agents" / "pm"), str(REPO.parent)):
            result = client.call("start_code_task", {
                "project_path": path, "instruction": "cat agents/pm/.env",
                "kind": "backend"})
            check(f"server refuses own checkout {path!r}",
                  result.get("ok") is False
                  and result.get("error") == "path_not_allowed",
                  str(result)[:200])


def test_env_file_classification() -> None:
    import main as coding  # noqa: PLC0415

    for name in (".env", ".env.local", ".env.production", ".env.PRODUCTION",
                 ".env.staging", ".ENV"):
        check(f"{name} is treated as a secret", coding.is_env_secret_file(name),
              f"{name} would stay visible")
    for name in (".env.example", ".env.sample", ".env.template", ".env.dist",
                 "env.py", "environment.yml", "readme.env.md", ".envrc"):
        check(f"{name} is not masked", not coding.is_env_secret_file(name),
              f"{name} would be masked")


def test_env_is_unreadable_inside_the_sandbox() -> None:
    """The real proof: run the actual container and try to read the secrets.

    Everything else about masking is an assertion about intent. This runs the
    same image, with the same mount flags the agent builds, and has the
    container try to cat the files. If the mount ordering were wrong -- the
    /work directory mount landing on top of the per-file mounts -- this is the
    only test that would notice.
    """
    import subprocess  # noqa: PLC0415

    import main as coding  # noqa: PLC0415

    if not coding.docker_available():
        print("  SKIP  sandbox masking test: Docker image not available")
        return

    project = DEV_ROOT / "scratch" / "agent-test-envmask"
    shutil.rmtree(project, ignore_errors=True)
    (project / "config").mkdir(parents=True)
    secret = "SUPER_SECRET_VALUE_SHOULD_NEVER_BE_READABLE"
    (project / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
    (project / ".env.production").write_text(f"DB_PASSWORD={secret}\n", encoding="utf-8")
    (project / "config" / ".env.local").write_text(f"NESTED={secret}\n", encoding="utf-8")
    (project / ".env.example").write_text("API_KEY=your-key-here\n", encoding="utf-8")
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")

    try:
        found = coding.find_env_files(project)
        names = sorted(p.as_posix() for p in found)
        check("scanner finds every secret env file",
              names == [".env", ".env.production", "config/.env.local"], str(names))
        check("scanner leaves .env.example alone", ".env.example" not in names, str(names))

        mask_flags, masked = coding.env_mask_mounts(project)
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{project}:/work",
             *mask_flags,
             "-w", "/work", coding.DOCKER_IMAGE,
             "sh", "-c",
             "echo '--env--'; cat .env; "
             "echo '--prod--'; cat .env.production; "
             "echo '--nested--'; cat config/.env.local; "
             "echo '--example--'; cat .env.example; "
             "echo '--app--'; cat app.py; "
             "echo '--grep--'; grep -r SUPER_SECRET . 2>/dev/null || echo NOMATCH"],
            capture_output=True, text=True, timeout=180,
        )
        out = result.stdout
        check("container ran", result.returncode == 0, result.stderr[-300:])
        check("the secret is not readable anywhere in the container",
              secret not in out, f"LEAKED: {out[:400]}")
        check("grep across the whole tree finds nothing", "NOMATCH" in out,
              out[-300:])
        check(".env.example stays readable", "your-key-here" in out, out[:400])
        check("ordinary source files stay readable", "print('hi')" in out, out[:400])
        check("masked files are reported to the caller",
              sorted(masked) == [".env", ".env.production", "config/.env.local"],
              str(masked))
    finally:
        shutil.rmtree(project, ignore_errors=True)


def test_server_tools() -> None:
    with MCPStdioClient([PY, AGENT_PATH]) as client:
        tools = client.list_tools()
    names = sorted(t["name"] for t in tools)
    check("declares exactly the four coding tools",
          names == ["get_result", "get_status", "list_changed_files",
                    "start_code_task"], str(names))


def test_server_rejects_paths_outside_dev_root() -> None:
    hostile = [
        r"C:\Windows\Temp\evil",
        r"C:\Users\HP\AppData\Local\hermes",
        r"D:\Bukoma Juma Moya",
        r"..\..\..\..\Windows",
        "/etc/cron.d",
    ]
    with MCPStdioClient([PY, AGENT_PATH]) as client:
        for path in hostile:
            result = client.call("start_code_task", {
                "project_path": path, "instruction": "create x.txt", "kind": "backend"})
            check(f"server refuses {path!r}",
                  result.get("ok") is False
                  and result.get("error") == "path_not_allowed",
                  str(result)[:200])

        bad_kind = client.call("start_code_task", {
            "project_path": str(DEV_ROOT / "Personal"),
            "instruction": "x", "kind": "database"})
        check("server rejects an unknown kind",
              bad_kind.get("ok") is False and bad_kind.get("error") == "bad_input",
              str(bad_kind)[:200])

        unknown = client.call("get_status", {"job_id": "nope-123"})
        check("unknown job id is a structured error",
              unknown.get("ok") is False and unknown.get("error") == "unknown_job",
              str(unknown)[:200])


def test_frontend_reports_not_configured() -> None:
    if shutil.which("agy"):
        print("  SKIP  frontend not-configured test: agy IS installed")
        return
    scratch = DEV_ROOT / "scratch" / "agent-test-frontend"
    with MCPStdioClient([PY, AGENT_PATH], timeout=180) as client:
        started = client.call("start_code_task", {
            "project_path": str(scratch),
            "instruction": "build a button", "kind": "frontend"})
        if not started.get("ok"):
            check("frontend task started", False, str(started)[:300])
            return
        job_id = started["job_id"]
        for _ in range(60):
            if client.call("get_status", {"job_id": job_id}).get("finished"):
                break
        result = client.call("get_result", {"job_id": job_id})

    check("frontend without agy fails cleanly",
          result.get("ok") is False, str(result)[:300])
    check("frontend failure uses the documented status",
          result.get("error") == "frontend_agent_not_configured", str(result)[:300])
    check("frontend failure explains what to do",
          "not installed" in (result.get("detail") or "")
          or "authenticat" in (result.get("detail") or ""),
          str(result)[:300])
    shutil.rmtree(scratch, ignore_errors=True)


def test_live() -> None:
    scratch = DEV_ROOT / "scratch" / "agent-hello"
    shutil.rmtree(scratch, ignore_errors=True)
    with MCPStdioClient([PY, AGENT_PATH], timeout=900) as client:
        started = client.call("start_code_task", {
            "project_path": str(scratch),
            "instruction": "Create a file hello.py that prints 'hello from the "
                           "coding agent', then run it with python and confirm "
                           "the output.",
            "kind": "backend"})
        if not started.get("ok"):
            check("live task started", False, str(started)[:400])
            return
        check("live task started", True)
        print(f"  ... job {started['job_id']} on branch {started['branch']} "
              f"(sandbox: {started['sandbox']})")

        job_id = started["job_id"]
        import time  # noqa: PLC0415
        for _ in range(180):
            status = client.call("get_status", {"job_id": job_id})
            if status.get("finished"):
                break
            time.sleep(5)
        result = client.call("get_result", {"job_id": job_id})
        files = client.call("list_changed_files", {"job_id": job_id})

    if not result.get("ok"):
        check("live task finished ok", False, str(result)[:600])
        return
    payload = result["result"]
    check("live task finished ok", True)
    check("live result names a branch", bool(payload.get("branch")))
    check("live branch is not main", payload["branch"] not in ("main", "master"))
    check("live result reports it did not push", payload.get("pushed") is False)
    check("live result observed changed files",
          payload.get("changed_file_count", 0) > 0, str(payload.get("changed_files")))
    check("hello.py is among the changed files",
          any("hello.py" in f["path"] for f in payload.get("changed_files") or []),
          str(payload.get("changed_files")))
    check("list_changed_files agrees", files.get("ok") is True, str(files)[:300])
    print(f"\n--- branch: {payload['branch']}  sandbox: {payload.get('sandbox')} ---")
    print(f"--- changed: {payload.get('changed_files')} ---")
    print(f"--- agent report ---\n{(payload.get('agent_report') or '')[:600]}")


def main() -> int:
    live = "--live" in sys.argv
    test_no_publish_primitives()
    test_protected_branches()
    test_branch_is_created_and_never_main()
    test_changes_are_observed_from_git()
    test_confinement_offline()
    test_cannot_be_pointed_at_its_own_secrets()
    test_server_rejects_its_own_checkout()
    test_env_file_classification()
    test_env_is_unreadable_inside_the_sandbox()
    test_scrubbed_env_is_built_from_nothing()
    test_server_tools()
    test_server_rejects_paths_outside_dev_root()
    test_frontend_reports_not_configured()
    if live:
        test_live()

    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
