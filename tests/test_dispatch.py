import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_autoharness import config
from codex_autoharness.hook import dispatch
from codex_autoharness.lib import counters, layer


@pytest.fixture(autouse=True)
def _unguard(monkeypatch):
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)  # ambient may set it


def _roots(tmp_path):
    return {layer.GLOBAL: tmp_path / "g", layer.PROJECT: tmp_path / "p"}


def test_unknown_event_is_ignored_safely(tmp_path):
    assert dispatch.dispatch({"hook_event_name": "Nope"}, roots=_roots(tmp_path))["ignored"] is True


def test_subagent_stop_is_ignored(tmp_path):
    # reflector completion is SubagentStop (E6 S4): never a turn, never counted
    assert dispatch.dispatch({"hook_event_name": "SubagentStop"}, roots=_roots(tmp_path))["ignored"] is True


def test_handler_exception_is_failsafe(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dispatch.counters, "bump_request", boom)
    out = dispatch.dispatch({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                             "turn_id": "t1"}, roots=_roots(tmp_path))
    assert "error" in out  # never propagates to crash the host hook


def test_run_identifiers_stay_distinct_for_unsafe_session_ids():
    first = dispatch._run_id({"session_id": "...", "count": 5})
    second = dispatch._run_id({"session_id": "???", "count": 5})
    assert first != second and first != "run-5"
    assert first.endswith("-5") and second.endswith("-5")


@pytest.mark.parametrize("event_name,curator", [("Stop", False), ("SessionEnd", False), ("Stop", True)])
def test_detached_launch_failure_is_reported(tmp_path, monkeypatch, event_name, curator):
    roots = _roots(tmp_path)
    monkeypatch.setattr(config, "REFLECT_EVERY_N", 2 if curator else 1)
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 1 if curator else 0)
    dispatch.dispatch({"hook_event_name": "PreToolUse", "session_id": "session"}, roots=roots)
    event = {"hook_event_name": event_name, "session_id": "session"}
    if not curator:
        assert "transcript" in dispatch.dispatch(event, roots=roots)["error"]
        assert counters.session_count("session", roots[layer.PROJECT]) == 1
    def fail(*args, **kwargs):
        if not curator:
            counters.bump_session("session", roots[layer.PROJECT])  # activity racing the failed launch
        raise OSError("no interpreter")
    monkeypatch.setattr(dispatch.subprocess, "Popen", fail)
    event["transcript_path"] = "/tmp/transcript"
    result = dispatch.dispatch(event, roots=roots)
    assert "no interpreter" in result["error"]
    assert counters.session_count("session", roots[layer.PROJECT]) == (1 if curator else 2)
    state = layer.state_dir(layer.PROJECT, roots[layer.PROJECT])
    assert counters._read_int(state / "last_curated_tool_count") == 0
    assert json.loads((state / "last_run.json").read_text())["error"] == "worker_launch_failure"

    launches = []
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **k: launches.append(a))
    assert "error" not in dispatch.dispatch(event, roots=roots)
    assert len(launches) == 1
    assert counters.session_count("session", roots[layer.PROJECT]) == (1 if curator else 0)
    assert counters._read_int(state / "last_curated_tool_count") == (1 if curator else 0)


def test_denied_reflector_write_reaches_the_host_as_deny_json(tmp_path, capsys):
    # end to end through the dispatcher: dispatch() decides, _emit is what the host actually reads
    dispatch._emit(dispatch.dispatch({"hook_event_name": "PreToolUse", "tool_name": "Write",
                                       "agent_type": "autoharness:reflector"}, roots=_roots(tmp_path)))
    hso = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "stage intents" in hso["permissionDecisionReason"]


def test_denied_child_write_reaches_the_host_as_deny_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(config.CHILD_SESSION_ENV, "1")
    dispatch._emit(dispatch.dispatch({"hook_event_name": "PreToolUse", "tool_name": "Write",
                                       "session_id": "s7"}, roots=_roots(tmp_path)))
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _interactive_intent(name="learned"):
    return {"action": "create", "name": name, "level": "project", "reason": "r", "evidence": "e",
            "body": f"---\nname: {name}\ndescription: Use when a learned thing applies.\n---\nRule.\n"}


def test_child_session_stop_leaves_the_interactive_queue_alone(tmp_path, monkeypatch):
    # a child has its own run id and spawn drains it; touching the user's queue from there would be
    # a reflector landing user proposals under the reflector's identity
    from codex_autoharness.lib import intent_queue
    roots = {"global": tmp_path / "g", "project": tmp_path / "p"}
    monkeypatch.setenv(config.CHILD_SESSION_ENV, "1")
    intent_queue.append(config.INTERACTIVE_RUN_ID, _interactive_intent(), roots["project"])
    dispatch.dispatch({"hook_event_name": "Stop", "session_id": "s1"}, roots=roots, reflect=lambda *a: None)
    assert len(list(intent_queue.read(config.INTERACTIVE_RUN_ID, roots["project"]))) == 1


def test_stop_with_empty_interactive_queue_writes_no_run_account(tmp_path, monkeypatch):
    # every turn drains; an empty drain must not leave a runs/ file or a last_run summary behind,
    # or the next session start would report a run that never happened
    from codex_autoharness.lib import layer
    roots = {"global": tmp_path / "g", "project": tmp_path / "p"}
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    dispatch.dispatch({"hook_event_name": "Stop", "session_id": "s1"}, roots=roots, reflect=lambda *a: None)
    state = layer.state_dir("project", roots["project"])
    assert not (state / "last_run.json").exists()
    assert not list((state / "runs").glob("*.json")) if (state / "runs").exists() else True


def _run_under_old_python(tmp_path, version=(3, 9, 6, "final", 0)):
    # the host's bare `python3` may be older than the 3.11 floor (Xcode ships 3.9.6 at
    # /usr/bin/python3); simulate it in a child by pinning version_info and hiding tomllib,
    # which is the stdlib module that entered in 3.11 and that redact.py imports at module level
    probe = tmp_path / "old_python_probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.version_info = {version!r}\n"
        "class _NoTomllib:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'tomllib':\n"
        "            raise ModuleNotFoundError(\"No module named 'tomllib'\", name='tomllib')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _NoTomllib())\n"
        "import codex_autoharness.hook.dispatch\n"
    )
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(probe)], capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "src")},
    )


def test_below_floor_python_exits_clean_instead_of_crashing_at_import(tmp_path):
    # hooks.json invokes bare `python3` for all four events, and every import in the chain is
    # module level, so an interpreter below the floor dies before dispatch() is entered and the
    # fail-safe handler never gets the chance to catch it: the whole plugin goes off silently
    r = _run_under_old_python(tmp_path)
    assert r.returncode == 0, f"expected a clean exit, got {r.returncode}:\n{r.stderr}"
    assert "Traceback" not in r.stderr
    assert "tomllib" not in r.stderr
    assert "3.11" in r.stderr and "3.9" in r.stderr and "hooks.json" in r.stderr


def test_queued_jobs_keep_session_models_and_end_uses_own_fallback(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    launched = []
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda argv, **kwargs: launched.append(list(argv)))
    monkeypatch.setattr(dispatch.on_stop, "on_stop", lambda event, **kwargs:
                        {"triggered": True, "session_id": event["session_id"], "count": 1})
    monkeypatch.setattr(dispatch.on_session_end, "on_session_end", lambda event, **kwargs:
                        {"triggered": True, "session_id": event["session_id"], "count": 1})
    monkeypatch.setattr(dispatch, "_curation_count", lambda root: 1)
    for session, model in (("first", "model-a"), ("second", "model-b"), ("first", "model-c")):
        event = {"hook_event_name": "Stop", "session_id": session, "model": model,
                 "transcript_path": str(tmp_path / f"{session}.jsonl")}
        dispatch.dispatch(event, roots=roots)
        assert "_autoharness_model_settings" not in event  # never mutate the supplied event
    dispatch.dispatch({"hook_event_name": "SessionEnd", "session_id": "second",
                       "transcript_path": str(tmp_path / "second.jsonl")}, roots=roots)
    assert [argv[argv.index("--model") + 1] for argv in launched] == [
        "model-a", "model-a", "model-b", "model-b", "model-c", "model-c", "model-b"]
