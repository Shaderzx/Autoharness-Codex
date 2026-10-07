"""Codex hook and rollout contracts at the dispatcher/capture boundaries."""

import json
import shlex
import subprocess
import sys

import pytest

from codex_autoharness import config
from codex_autoharness.hook import capture, dispatch
from codex_autoharness.lib import counters, layer, sidecar, skill_store


@pytest.fixture(autouse=True)
def main_session(monkeypatch):
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 0)


@pytest.fixture
def roots(tmp_path):
    return {layer.GLOBAL: tmp_path / "global", layer.PROJECT: tmp_path / "project"}


def managed_skill(roots, name="native-reader"):
    root = roots[layer.PROJECT]
    skill_store.write_body(layer.PROJECT, name, "---\nname: native-reader\n"
                           "description: Read project conventions.\n---\nUse project conventions.\n", root)
    sidecar.create(layer.PROJECT, name, anchor=0, root=root)
    return layer.symbol_dir(layer.PROJECT, name, root) / "SKILL.md"


def post_tool(roots, name, tool_input, response=None):
    return dispatch.dispatch({
        "hook_event_name": "PostToolUse",
        "session_id": "codex-session",
        "turn_id": "turn-1",
        "tool_name": name,
        "tool_input": tool_input,
        "tool_response": {"exit_code": 0} if response is None else response,
    }, roots=roots)


def test_prompt_submission_counts_each_turn_once_and_stop_does_not(roots):
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "codex-session",
             "turn_id": "turn-1", "prompt": "Fix the parser."}
    dispatch.dispatch(event, roots=roots)
    dispatch.dispatch(event, roots=roots)
    dispatch.dispatch({**event, "turn_id": "turn-2"}, roots=roots)
    dispatch.dispatch({"hook_event_name": "Stop", "session_id": "codex-session"},
                      roots=roots, reflect=lambda *args: None)
    for level, root in roots.items():
        assert counters.request_count(level, root) == 2


def test_home_session_shared_root_counts_and_indexes_once(tmp_path):
    roots = {layer.GLOBAL: tmp_path / ".agents", layer.PROJECT: tmp_path / ".agents"}
    skill = managed_skill(roots)
    dispatch.dispatch({"hook_event_name": "UserPromptSubmit", "session_id": "home-session",
                       "turn_id": "t1", "prompt": "Read conventions"}, roots=roots)
    assert counters.request_count(layer.GLOBAL, roots[layer.GLOBAL]) == 1
    assert skill_store.find("native-reader", roots) == layer.GLOBAL
    verdict = dispatch.dispatch({"hook_event_name": "SessionStart"}, roots=roots)
    assert verdict["result"]["context"].count("- native-reader") == 1
    post_tool(roots, "Read", {"file_path": str(skill)})
    assert sidecar.read(layer.GLOBAL, "native-reader", roots[layer.GLOBAL])["use"] == 1


def test_native_tool_lifecycle_counts_activity_once(roots):
    event = {"session_id": "codex-session", "turn_id": "turn-1",
             "tool_name": "exec_command", "tool_input": {"cmd": "pwd"}}
    dispatch.dispatch({**event, "hook_event_name": "PreToolUse"}, roots=roots)
    dispatch.dispatch({**event, "hook_event_name": "PostToolUse",
                       "tool_response": {"exit_code": 0}}, roots=roots)
    assert counters.session_count("codex-session", roots[layer.PROJECT]) == 1


def test_same_turn_id_in_different_sessions_counts_both_requests(roots):
    for session_id in ("first-session", "second-session"):
        dispatch.dispatch({"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                           "turn_id": "turn-1", "prompt": "Inspect the parser."}, roots=roots)
    for level, root in roots.items():
        assert counters.request_count(level, root) == 2


def test_heavy_turn_crosses_curation_threshold_once_and_keeps_aggregate(roots, monkeypatch):
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 3)
    monkeypatch.setattr(config, "REFLECT_EVERY_N", 2)
    fired = []
    tool = {"hook_event_name": "PreToolUse", "session_id": "codex-session",
            "tool_name": "exec_command", "tool_input": {"cmd": "pwd"}}
    stop = {"hook_event_name": "Stop", "session_id": "codex-session"}

    def end_turn():
        dispatch.dispatch(stop, roots=roots, reflect=lambda *args: None,
                          consolidate=lambda run_id, _: fired.append(run_id))

    for _ in range(5):
        dispatch.dispatch(tool, roots=roots)
    end_turn()
    end_turn()
    assert fired == ["codex-session-c5"]
    assert counters.session_count("codex-session", roots[layer.PROJECT]) == 0

    for _ in range(2):
        dispatch.dispatch(tool, roots=roots)
    end_turn()
    assert fired == ["codex-session-c5"]
    dispatch.dispatch(tool, roots=roots)
    end_turn()
    assert fired == ["codex-session-c5", "codex-session-c8"]


def test_disabled_curation_does_not_fire_after_tool_activity(roots):
    for _ in range(5):
        dispatch.dispatch({"hook_event_name": "PreToolUse", "session_id": "codex-session",
                           "tool_name": "exec_command"}, roots=roots)
    dispatch.dispatch({"hook_event_name": "Stop", "session_id": "codex-session"},
                      roots=roots, reflect=lambda *args: None,
                      consolidate=lambda *args: pytest.fail("disabled curator launched"))


def test_detached_worker_imports_from_an_unrelated_directory(roots, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    launches = []
    with monkeypatch.context() as patcher:
        patcher.setattr(dispatch.subprocess, "Popen",
                        lambda *args, **kwargs: launches.append((args, kwargs)))
        assert dispatch._detached_launch("rollout.jsonl", "codex-session", "run-1", roots) is None
    assert len(launches) == 1
    env = launches[0][1]["env"]
    probe = subprocess.run(
        [sys.executable, "-c", "import codex_autoharness.hook.spawn; print('worker-import-ok')"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "worker-import-ok"


@pytest.mark.parametrize("tool,input_key", [
    ("Read", "file_path"),
    ("mcp__lean_ctx__ctx_read", "path"),
])
def test_native_successful_skill_read_counts_use(roots, tool, input_key):
    path = managed_skill(roots)
    post_tool(roots, tool, {input_key: str(path)})
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 1


@pytest.mark.parametrize("tool,input_key,command", [
    ("exec_command", "cmd", "cat {path}"),
    ("Bash", "command", "sed -n '1,80p' {path}"),
    ("exec_command", "cmd", "head -80 {path}"),
    ("exec_command", "cmd", "tail -20 {path}"),
    ("exec_command", "cmd", "rg conventions {path}"),
])
def test_shell_skill_reads_count_use(roots, tool, input_key, command):
    path = managed_skill(roots)
    post_tool(roots, tool, {input_key: command.format(path=shlex.quote(str(path)))})
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 1


def test_supporting_file_read_counts_view(roots):
    path = managed_skill(roots).parent / "references" / "guide.md"
    path.parent.mkdir()
    path.write_text("Use the existing parser.\n")
    post_tool(roots, "mcp__lean_ctx__ctx_read", {"path": str(path)})
    metadata = sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])
    assert metadata["view"] == 1
    assert metadata["use"] == 0


@pytest.mark.parametrize("response", [
    {"exit_code": 1},
    {"isError": True},
    json.dumps({"exit_code": 2, "output": "missing file"}),
])
def test_failed_read_does_not_count_skill_use(roots, response):
    path = managed_skill(roots)
    post_tool(roots, "exec_command", {"cmd": "cat " + shlex.quote(str(path))}, response)
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 0


def test_skill_path_in_echo_text_is_not_a_read(roots):
    path = managed_skill(roots)
    post_tool(roots, "exec_command", {"cmd": "echo " + shlex.quote(str(path))})
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 0


@pytest.mark.parametrize("template", ["cat > {path}", "rg {path} another-file", "rg --files {path}"])
def test_shell_path_references_are_not_automatically_skill_reads(roots, template):
    path = managed_skill(roots)
    post_tool(roots, "exec_command", {"cmd": template.format(path=shlex.quote(str(path)))})
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 0


def test_child_hooks_do_not_increment_activity_requests_or_skill_usage(roots, monkeypatch):
    path = managed_skill(roots)
    monkeypatch.setenv(config.CHILD_SESSION_ENV, "1")
    for event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"):
        dispatch.dispatch({"hook_event_name": event_name, "session_id": "codex-child",
                           "turn_id": "child-turn", "tool_name": "Read",
                           "tool_input": {"file_path": str(path)}, "tool_response": {}},
                          roots=roots, reflect=lambda *args: pytest.fail("child reflection launched"))
    assert counters.session_count("codex-child", roots[layer.PROJECT]) == 0
    assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 0
    for level, root in roots.items():
        assert counters.request_count(level, root) == 0


def test_child_session_start_does_not_consume_main_session_summary(roots, monkeypatch):
    summary = layer.state_dir(layer.PROJECT, roots[layer.PROJECT]) / "last_run.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"landed": 1, "rejected": 0}))
    monkeypatch.setenv(config.CHILD_SESSION_ENV, "1")
    dispatch.dispatch({"hook_event_name": "SessionStart", "session_id": "codex-child"},
                      roots=roots)
    assert summary.exists()


@pytest.mark.parametrize("event", [[], None, "not an event", 42])
def test_malformed_native_hook_input_is_ignored(roots, event):
    result = dispatch.dispatch(event, roots=roots)
    assert result.get("ignored") or result.get("error")


def test_native_rollout_digest_keeps_messages_and_tool_names_only(tmp_path):
    records = [
        {"type": "session_meta", "payload": {"id": "codex-session"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Fix the parser."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "I will inspect the parser."}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
         "call_id": "call-1", "arguments": json.dumps({"cmd": "private-argument"})}},
        {"type": "response_item", "payload": {"type": "function_call_output",
         "call_id": "call-1", "output": "large-private-tool-output"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Added the missing delimiter."}]}},
    ]
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    original = transcript.read_bytes()
    digest = capture.digest(transcript, len(original))
    assert "user: Fix the parser." in digest
    assert "assistant: I will inspect the parser." in digest
    assert "exec_command" in digest
    assert "Added the missing delimiter." in digest
    assert "private-argument" not in digest
    assert "large-private-tool-output" not in digest
    assert transcript.read_bytes() == original
