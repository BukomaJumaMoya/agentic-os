"""Redirect runtime approval/evidence state to a throwaway directory.

MUST be imported BEFORE any ``orchestrator`` import.

``orchestrator/approval.py`` and ``orchestrator/flagship.py`` both evaluate
``os.getenv("AGENTIC_STATE_DIR")`` at module level, so STATE_ROOT / APPROVAL_DIR
/ EVIDENCE_DIR are frozen the moment those modules are first imported. Setting
the environment variable afterwards has no effect.

Audit F-23: every suite's setup()/teardown() called ``shutil.rmtree`` on the
repository's real ``.approval/`` and ``evidence/`` directories, so running the
tests destroyed live state -- pending approval requests, proposal evidence, and
the cached conversationRef. Pointing the constants at a temp dir makes those
same rmtree calls correct instead of destructive; the suites' teardown logic is
deliberately left unchanged.

Uses ``tempfile.mkdtemp`` rather than pytest's ``tmp_path`` fixture because
these suites run as plain scripts (``python tests/foo.py``), where no pytest
fixture exists. This works under both plain execution and pytest.
"""

# Blocks outbound delivery for this process and every agent subprocess. State
# isolation and send blocking are the same requirement -- a suite that must not
# touch real approval files must not touch a real phone either.
import _no_outbound  # noqa: F401

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="agentic-test-state-")

# Set before any orchestrator module is imported -- see module docstring.
os.environ["AGENTIC_STATE_DIR"] = _tmp

STATE_ROOT = Path(_tmp)
APPROVAL_DIR = STATE_ROOT / ".approval"
EVIDENCE_DIR = STATE_ROOT / "evidence"
APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

atexit.register(lambda: shutil.rmtree(_tmp, ignore_errors=True))
