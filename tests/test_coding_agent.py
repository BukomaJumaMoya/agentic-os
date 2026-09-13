#!/usr/bin/env python3
"""
Coding agent — offline tests.

Stubs orchestrator.llm.complete, the single socket-opening function, exactly as
the flagship unit suite does. No network, no API key, no model spend. Everything
above that boundary runs for real: prompt assembly, complete_json's parsing,
_validate, and the in-process ast.parse syntax check.

Workspace confinement has its own dedicated suite
(test_p0_1_workspace_confinement.py); the tests here cover the parts of the
write path that changed, namely that what gets written is the GENERATED code.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Point the workspace at a throwaway directory BEFORE importing the agent --
# WORKSPACE_ROOT is resolved at module load.
_TMP = Path(tempfile.mkdtemp(prefix="coding-agent-test-"))
os.environ["CODING_AGENT_WORKSPACE"] = str(_TMP)

from orchestrator import llm  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "coding_agent", BASE / "agents" / "coding" / "main.py")
coding = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coding)

passed = 0


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


VALID = {
    "summary": "Adds a guard so an empty sequence returns zero instead of raising.",
    "code": "def average(values):\n    if not values:\n        return 0\n    return sum(values) / len(values)\n",
    "language": "python",
    "components": [],
    "findings": [{"severity": "high",
                  "detail": "Division by zero when values is empty.",
                  "location": "return total / len(values)"}],
    "assumptions": ["An empty input should yield 0 rather than raise."],
    "unknowns": [],
}


class _Stub:
    def __init__(self, text=None, raises=None, model="stub/code-model"):
        self.text = text
        self.raises = raises
        self.model = model
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = llm.complete

        def fake(system, user, **kwargs):
            self.calls.append({"system": system, "user": user, "kwargs": kwargs})
            if self.raises is not None:
                raise self.raises
            return {"text": self.text, "model": self.model, "provider": "stub",
                    "attempts": [{"provider": "stub", "outcome": "ok"}],
                    "usage": {"total_tokens": 0}, "raw": {"stub": True}}

        llm.complete = fake
        return self

    def __exit__(self, *exc):
        llm.complete = self._real
        return False


def _reply(**overrides):
    d = dict(VALID)
    d.update(overrides)
    return json.dumps(d)


def test_explain_calls_the_model_and_does_not_echo_input():
    """The old stub returned code[:500] of its own input."""
    marker = "def unique_marker_function():\n    return 1\n"
    with _Stub(_reply(code="", findings=[])) as stub:
        reply, code, verification, result = coding.run_action(
            {"action": "explain", "code": marker, "language": "python"})
    assert reply["summary"]
    assert code == ""
    # The input reached the model rather than being handed straight back.
    assert marker in stub.calls[0]["user"]
    assert reply["summary"] != marker
    ok("explain_calls_the_model_and_does_not_echo_input")


def _feasibility_reply(**overrides):
    """Flat reply shape, as the loosened feasibility schema asks for."""
    d = {
        "summary": "The work splits into ingestion, scheduling and dispatch.",
        "components": ["Importer - reads the spreadsheet",
                       "Dispatcher - sends the messages"],
        "risks": ["Importer: concurrent edits to the sheet lose rows"],
        "assumptions": ["The spreadsheet stays the source of truth for now."],
        "unknowns": ["Which channels are actually required."],
    }
    d.update(overrides)
    return json.dumps(d)


def test_feasibility_accepts_the_flat_shape():
    with _Stub(_feasibility_reply()):
        reply, code, _, _ = coding.run_action(
            {"action": "feasibility", "prompt": "a system", "language": "python"})
    assert code == ""
    # Flat strings are rebuilt into the internal structure.
    assert reply["components"][0]["name"] == "Importer"
    assert reply["components"][0]["purpose"] == "reads the spreadsheet"
    assert reply["findings"][0]["location"] == "Importer"
    ok("feasibility_accepts_the_flat_shape")


def test_feasibility_requires_components_and_forbids_code():
    with _Stub(_feasibility_reply(components=[])):
        try:
            coding.run_action({"action": "feasibility", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "component breakdown" in str(e)
        else:
            raise AssertionError("feasibility with no components accepted")

    with _Stub(_feasibility_reply(code="print(1)")):
        try:
            coding.run_action({"action": "feasibility", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "must not return code" in str(e)
        else:
            raise AssertionError("feasibility returning code accepted")
    ok("feasibility_requires_components_and_forbids_code")


def test_feasibility_risk_must_name_a_component():
    """No unbound risk may reach the proposal.

    Enforced by dropping it, not by rejecting the whole reply: discarding five
    good components because the sixth risk was phrased loosely is the same
    brittleness as failing a proposal over a three-character subject. The drop
    is counted so a model that routinely fails to bind stays visible.
    """
    with _Stub(_feasibility_reply(
            risks=["Importer: concurrent edits lose rows",
                   "something might go wrong somewhere"])):
        reply, _, _, _ = coding.run_action(
            {"action": "feasibility", "prompt": "x", "language": "python"})
    assert len(reply["findings"]) == 1
    assert reply["findings"][0]["location"] == "Importer"
    assert reply["dropped_unbound_risks"] == 1
    # Every surviving risk names a component.
    assert all(f["location"] for f in reply["findings"])
    ok("feasibility_risk_must_name_a_component")


def test_feasibility_still_rejects_execution_claims():
    """Loosening the schema must not loosen the content rules."""
    with _Stub(_feasibility_reply(summary="I ran the integration and it works end to end.")):
        try:
            coding.run_action({"action": "feasibility", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "claims code was run" in str(e)
        else:
            raise AssertionError("execution claim accepted under the flat schema")
    with _Stub(_feasibility_reply(components=["Importer - TODO decide this"])):
        try:
            coding.run_action({"action": "feasibility", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "placeholder" in str(e)
        else:
            raise AssertionError("placeholder accepted under the flat schema")
    ok("feasibility_still_rejects_execution_claims")


def test_malformed_components_are_rejected():
    bad = [{"name": "", "purpose": "x"}]
    with _Stub(_reply(components=bad)):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "component has no name" in str(e)
        else:
            raise AssertionError("nameless component accepted")
    ok("malformed_components_are_rejected")


def test_generation_evidence_is_recorded():
    with _Stub(_reply()):
        _, _, _, result = coding.run_action(
            {"action": "generate", "prompt": "a thing", "language": "python"})
    assert result["provider"] == "stub"
    assert result["model"] == "stub/code-model"
    assert coding.PROMPT_VERSION == "coding/v1"
    ok("generation_evidence_is_recorded")


def test_code_action_must_return_code():
    with _Stub(_reply(code="")):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
            assert "must return code" in str(e)
        else:
            raise AssertionError("empty code accepted for a code action")
    ok("code_action_must_return_code")


def test_generated_python_must_parse():
    with _Stub(_reply(code="def broken(:\n    pass\n")):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
            assert "not syntactically valid" in str(e)
        else:
            raise AssertionError("unparseable Python accepted")
    ok("generated_python_must_parse")


def test_placeholder_markers_are_rejected():
    with _Stub(_reply(code="def f():\n    # TODO: implement\n    return None\n")):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "placeholder" in str(e)
        else:
            raise AssertionError("placeholder marker accepted")
    ok("placeholder_markers_are_rejected")


def test_credential_shaped_output_is_rejected():
    leaked = 'KEY = "gsk_abcdefghijklmnopqrstuvwxyz012345"\n'
    with _Stub(_reply(code=leaked)):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "credential" in str(e)
        else:
            raise AssertionError("credential-shaped literal accepted")
    ok("credential_shaped_output_is_rejected")


def test_claiming_to_have_run_code_is_rejected():
    """Nothing in this agent executes code, so the claim is always false."""
    with _Stub(_reply(summary="I ran the tests and they all pass without errors.")):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert "claims code was run" in str(e)
        else:
            raise AssertionError("false execution claim accepted")
    ok("claiming_to_have_run_code_is_rejected")


def test_findings_need_a_location():
    bad = [{"severity": "high", "detail": "something is wrong", "location": ""}]
    with _Stub(_reply(findings=bad, code="")):
        try:
            coding.run_action({"action": "review", "code": "x = 1", "language": "python"})
        except llm.LLMError as e:
            assert "no location" in str(e)
        else:
            raise AssertionError("finding without a location accepted")
    ok("findings_need_a_location")


def test_invalid_severity_is_rejected():
    bad = [{"severity": "catastrophic", "detail": "d", "location": "l"}]
    with _Stub(_reply(findings=bad, code="")):
        try:
            coding.run_action({"action": "review", "code": "x = 1", "language": "python"})
        except llm.LLMError as e:
            assert "invalid severity" in str(e)
        else:
            raise AssertionError("invalid severity accepted")
    ok("invalid_severity_is_rejected")


def test_unparseable_reply_fails_loudly():
    with _Stub("Sure! Here's the code you asked for."):
        try:
            coding.run_action({"action": "generate", "prompt": "x", "language": "python"})
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
        else:
            raise AssertionError("prose reply accepted")
    ok("unparseable_reply_fails_loudly")


def test_non_python_syntax_is_reported_unverified_not_claimed_ok():
    with _Stub(_reply(code="const x = 1;", language="javascript")):
        _, _, verification, _ = coding.run_action(
            {"action": "generate", "prompt": "x", "language": "javascript"})
    check = verification["generated_syntax"]
    assert check["status"] == "skipped", check
    assert "not verified" in check["reason"]
    ok("non_python_syntax_is_reported_unverified_not_claimed_ok")


def test_oversized_input_is_refused_before_any_model_call():
    err = coding.validate_input(
        {"action": "generate", "prompt": "x" * (coding.MAX_INPUT_CHARS + 1)})
    assert err and "above the" in err
    ok("oversized_input_is_refused_before_any_model_call")


def test_forbidden_action_is_refused():
    for action in coding.FORBIDDEN_ACTIONS:
        err = coding.validate_input({"action": action, "prompt": "x"})
        assert err and "forbidden" in err, action
    ok("forbidden_action_is_refused")


def test_write_target_stays_in_workspace():
    """Confinement is covered fully by the P0-1 suite; this is the sanity tie-in."""
    target = coding.resolve_write_target("out", "module.py")
    assert coding.WORKSPACE_ROOT in target.parents
    try:
        coding.resolve_write_target(".", "/etc/passwd")
    except ValueError:
        pass
    else:
        raise AssertionError("absolute path accepted")
    ok("write_target_stays_in_workspace")


def test_agent_never_executes_generated_code():
    """Guard against a future change reintroducing execution."""
    source = (BASE / "agents" / "coding" / "main.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]  # skip the module docstring
    for forbidden in ("exec(", "eval(", "os.system", "popen", "importlib",
                      "__import__", "pytest", "unittest.main", "runpy"):
        assert forbidden not in body, f"{forbidden} appeared in the coding agent"
    # The only subprocess calls are the pre-existing caller-path syntax checks.
    assert body.count("subprocess.run") == 2
    assert "py_compile" in body and "node" in body
    ok("agent_never_executes_generated_code")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL CODING AGENT TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
