"""Native hook subprocesses plus a local fake Codex executable; no API calls."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codex_autoharness.lib import (
    counters,
    intent_queue,
    layer,
    ledger,
    sidecar,
    skill_store,
)


def skill_body(name="date-rule", category="python"):
    return (f"---\nname: {name}\ndescription: Use when formatting dates.\n"
            f"category: {category}\n---\n# Date formatting\nUse datetime.isoformat().\n")


def proposal(name="date-rule", category="python"):
    return {"action": "create", "name": name, "level": "project", "body": skill_body(name, category),
            "reason": "Repeated date-formatting correction.",
            "evidence": "The project consistently uses ISO dates."}


@pytest.fixture
def sandbox(tmp_path):
    project, home = tmp_path / "project with spaces", tmp_path / "home"
    project.mkdir()
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "fake-model"\n')
    fake = tmp_path / "fake-codex"
    fake.write_text(f"#!{sys.executable}\n" + """
import json
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
bundle = sys.stdin.read()
observation = {
    'argv': arguments,
    'bundle': bundle,
    'child': os.environ.get('CODEX_AUTOHARNESS_CHILD_SESSION'),
    'config': (Path(os.environ['CODEX_HOME']) / 'config.toml').read_text(),
}
Path(os.environ['AUTOHARNESS_TEST_OBSERVATION']).write_text(json.dumps(observation))
if os.environ.get('AUTOHARNESS_TEST_BARRIER'):
    barrier = Path(os.environ['AUTOHARNESS_TEST_BARRIER'])
    (barrier / os.environ['CODEX_AUTOHARNESS_RUN_ID']).touch()
    deadline = time.monotonic() + 8
    while len(list(barrier.iterdir())) < 3:
        if time.monotonic() > deadline:
            raise SystemExit('session workers were serialized during inference')
        time.sleep(.02)
output = Path(arguments[arguments.index('--output-last-message') + 1])
output.write_text(Path(os.environ['AUTOHARNESS_TEST_PROPOSAL']).read_text())
""")
    fake.chmod(0o700)
    response = tmp_path / "fake-proposal.json"
    required = ("action", "name", "level", "body", "old_string", "new_string", "reason",
                "evidence", "files", "path", "absorbed_into")
    response.write_text(json.dumps({"intents": [{key: proposal().get(key) for key in required}]}))
    observation = tmp_path / "child-observation.json"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CODEX_AUTOHARNESS_")}
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "CODEX_HOME": str(home / ".codex"),
        "CODEX_AUTOHARNESS_CODEX_BIN": str(fake),
        "CODEX_AUTOHARNESS_REFLECT_EVERY_N": "1",
        "CODEX_AUTOHARNESS_CONSOLIDATE_EVERY_N": "0",
        "CODEX_AUTOHARNESS_NOTIFY": "",
        "CODEX_AUTOHARNESS_NOTIFY_CMD": "",
        "AUTOHARNESS_TEST_PROPOSAL": str(response),
        "AUTOHARNESS_TEST_OBSERVATION": str(observation),
    })
    roots = {layer.PROJECT: project / ".agents", layer.GLOBAL: home / ".agents"}
    return {"project": project, "home": home, "roots": roots, "env": env,
            "observation": observation, "response": response}


def cli(sandbox, *args, payload=None, raw=None, expected=0):
    command = [sys.executable, "-m", "codex_autoharness",
               "--project", str(sandbox["project"]), "--home", str(sandbox["home"]), *args]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else raw,
                            text=True, capture_output=True, env=sandbox["env"],
                            cwd=sandbox["project"], timeout=15)
    assert result.returncode == expected, (result.stdout, result.stderr)
    return result


def hook(sandbox, event, **fields):
    payload = {"hook_event_name": event, "session_id": "native-session", "turn_id": "turn-1",
               "cwd": str(sandbox["project"]), **fields}
    if "hook_commands" in sandbox:
        env = dict(sandbox["env"])
        env.pop("PYTHONPATH", None)
        result = subprocess.run(shlex.split(sandbox["hook_commands"][event]), input=json.dumps(payload),
                                text=True, capture_output=True, env=env,
                                cwd=sandbox["project"], timeout=15)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)
    return json.loads(cli(sandbox, "_hook", payload=payload).stdout)


def await_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.025)
    pytest.fail(f"Background worker did not produce {path.name}")


def test_real_hook_processes_learn_recall_attribute_and_restore(sandbox, tmp_path):
    installed = json.loads(cli(sandbox, "install").stdout)
    assert installed["ok"]
    native_hooks = json.loads(Path(installed["hooks"]).read_text())["hooks"]
    sandbox["hook_commands"] = {event: groups[0]["hooks"][0]["command"]
                                for event, groups in native_hooks.items()}
    secret = "ghp_" + "f" * 36
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text":
        "The project consistently uses ISO dates. " + secret}]}}) + "\n")
    original = transcript.read_bytes()
    root = sandbox["roots"][layer.PROJECT]

    assert hook(sandbox, "SessionStart") == {}
    assert hook(sandbox, "UserPromptSubmit", prompt="Format these dates.") == {}
    assert hook(sandbox, "UserPromptSubmit", prompt="Format these dates.") == {}
    tool = {"tool_name": "exec_command", "tool_input": {"cmd": "python date_check.py"}}
    assert hook(sandbox, "PreToolUse", **tool) == {}
    assert hook(sandbox, "PostToolUse", tool_response={"exit_code": 0}, **tool) == {}
    assert hook(sandbox, "Stop", transcript_path=str(transcript)) == {}

    summary_path = layer.state_dir(layer.PROJECT, root) / "last_run.json"
    await_file(summary_path)
    summary = json.loads(summary_path.read_text())
    assert summary["landed"] == 1, summary
    assert skill_store.read_body(layer.PROJECT, "date-rule", root) == skill_body()
    assert sidecar.is_agent_created(layer.PROJECT, "date-rule", root)
    assert counters.request_count(layer.PROJECT, root) == 1
    assert counters.request_count(layer.GLOBAL, sandbox["roots"][layer.GLOBAL]) == 1
    assert counters.session_count("native-session", root) == 0
    assert transcript.read_bytes() == original
    observed = json.loads(sandbox["observation"].read_text())
    assert secret not in observed["bundle"]
    assert "[REDACTED:" in observed["bundle"]
    assert observed["child"] == "1"
    assert observed["argv"][0] == "exec"
    assert observed["argv"][observed["argv"].index("--sandbox") + 1] == "read-only"
    assert 'approval_policy = "never"' in observed["config"]

    context = hook(sandbox, "SessionStart", session_id="recall-session")["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert "landed 1" in context["additionalContext"]
    assert "## python" in context["additionalContext"]
    assert "date-rule [project]" in context["additionalContext"]
    assert not summary_path.exists()

    skill_path = skill_store.skill_path(layer.PROJECT, "date-rule", root)
    read = {"tool_name": "exec_command", "tool_input": {"cmd": "cat " + shlex.quote(str(skill_path))}}
    hook(sandbox, "PreToolUse", **read)
    assert sidecar.read(layer.PROJECT, "date-rule", root)["use"] == 0
    hook(sandbox, "PostToolUse", tool_response={"exit_code": 1}, **read)
    assert sidecar.read(layer.PROJECT, "date-rule", root)["use"] == 0
    hook(sandbox, "PostToolUse", tool_response={"exit_code": 0}, **read)
    assert sidecar.read(layer.PROJECT, "date-rule", root)["use"] == 1

    before = {p.relative_to(skill_path.parent): p.read_bytes()
              for p in skill_path.parent.rglob("*") if p.is_file()}
    assert json.loads(cli(sandbox, "archive", "date-rule").stdout)["ok"]
    assert not skill_path.exists()
    assert "date-rule [project]" not in cli(sandbox, "index").stdout
    assert json.loads(cli(sandbox, "restore", "date-rule").stdout)["ok"]
    after = {p.relative_to(skill_path.parent): p.read_bytes()
             for p in skill_path.parent.rglob("*") if p.is_file()}
    assert before == after


def test_concurrent_sessions_inherit_distinct_models_without_serializing_inference(sandbox, tmp_path):
    barrier = tmp_path / "concurrent-workers"
    barrier.mkdir()
    runs = []
    for index, model in enumerate(("session-a-model", "session-b-model", "session-c-model")):
        current = {**sandbox, "env": dict(sandbox["env"])}
        observation = tmp_path / f"observation-{index}.json"
        response = tmp_path / f"response-{index}.json"
        content = json.loads(sandbox["response"].read_text())
        content["intents"][0].update(name=f"lesson-{index}", body=skill_body(f"lesson-{index}"))
        response.write_text(json.dumps(content))
        current["env"].update(AUTOHARNESS_TEST_BARRIER=str(barrier),
                              AUTOHARNESS_TEST_OBSERVATION=str(observation),
                              AUTOHARNESS_TEST_PROPOSAL=str(response))
        transcript = tmp_path / f"transcript-{index}.jsonl"
        transcript.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text",
            "text": "The project consistently uses ISO dates."}]}}) + "\n")
        session = f"parallel-{index}"
        hook(current, "SessionStart", session_id=session, model=model)
        hook(current, "PreToolUse", session_id=session, tool_name="Bash", tool_input={"command": "pwd"})
        hook(current, "Stop", session_id=session, model=model, transcript_path=str(transcript))
        runs.append((observation, model, session, transcript))
    root = sandbox["roots"][layer.PROJECT]
    for index, (observation, model, session, transcript) in enumerate(runs):
        await_file(skill_store.skill_path(layer.PROJECT, f"lesson-{index}", root))
        await_file(root / "codex-autoharness" / f"offset-{session}")
        arguments = json.loads(observation.read_text())["argv"]
        assert arguments[arguments.index("--model") + 1] == model
        assert counters.session_offset(session, root) == transcript.stat().st_size
    assert len(list(barrier.iterdir())) == 3


def test_interactive_stage_is_drained_by_stop_and_grouped_index_recalled(sandbox):
    root = sandbox["roots"][layer.PROJECT]
    for name, category in (("z-date", "python"), ("a-date", "python"), ("shell-date", "shell")):
        out = json.loads(cli(sandbox, "stage", "--queue-only", payload=proposal(name, category)).stdout)
        assert out["ok"]
        assert not skill_store.exists(layer.PROJECT, name, root)
    assert len(intent_queue.read("interactive", root)) == 3

    assert hook(sandbox, "Stop") == {}

    assert intent_queue.read("interactive", root) == []
    index = cli(sandbox, "index").stdout
    assert index.index("## python") < index.index("## shell")
    assert index.index("a-date [project]") < index.index("z-date [project]")
    history = json.loads(cli(sandbox, "history", "a-date").stdout)
    assert history["ok"] and history["entries"][0]["action"] == "create"
    assert ledger.read(layer.PROJECT, "a-date", root)[0]["evidence"].startswith("references/evidence-")


def test_lifecycle_eviction_uses_real_prompt_and_read_events(sandbox):
    sandbox["env"]["CODEX_AUTOHARNESS_MATURITY_PROJECT"] = "2"
    sandbox["env"]["CODEX_AUTOHARNESS_CAPACITY_PROJECT"] = "1"
    root = sandbox["roots"][layer.PROJECT]
    for name in ("frequent-date", "unused-date"):
        assert json.loads(cli(sandbox, "stage", payload=proposal(name)).stdout)["ok"]
    frequent = skill_store.skill_path(layer.PROJECT, "frequent-date", root)
    hook(sandbox, "PostToolUse", tool_name="mcp__lean_ctx__ctx_read",
         tool_input={"paths": [str(frequent), str(frequent)]}, tool_response={"content": []})
    for turn in ("one", "two"):
        hook(sandbox, "UserPromptSubmit", turn_id=turn, prompt="Format a date.")

    context = hook(sandbox, "SessionStart")["hookSpecificOutput"]["additionalContext"]

    assert "frequent-date [project]" in context
    assert "unused-date [project]" not in context
    assert frequent.exists()
    assert not skill_store.exists(layer.PROJECT, "unused-date", root)
    assert (layer.archive_dir(layer.PROJECT, root) / "unused-date" / "SKILL.md").exists()
    assert json.loads(cli(sandbox, "restore", "unused-date").stdout)["ok"]


def test_disabled_hooks_do_not_create_state(sandbox):
    sandbox["env"]["CODEX_AUTOHARNESS_ENABLED"] = "0"
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"):
        assert hook(sandbox, event, tool_name="exec_command", tool_input={"cmd": "pwd"}) == {}
    assert not sandbox["roots"][layer.PROJECT].exists()
    assert not sandbox["roots"][layer.GLOBAL].exists()
    assert not sandbox["observation"].exists()


@pytest.mark.parametrize("raw", ["null", "[]", "42", "not JSON"])
def test_malformed_hook_stdin_is_a_successful_noop(sandbox, raw):
    assert json.loads(cli(sandbox, "_hook", raw=raw).stdout) == {}
    assert not sandbox["roots"][layer.PROJECT].exists()


def test_native_mcp_transport_survives_invalid_request_then_stages(sandbox):
    requests = ["not JSON", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "stage_skill", "arguments": proposal()}})]
    result = cli(sandbox, "mcp", raw="\n".join(requests) + "\n")
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["id"] == 1 and "capabilities" in responses[1]["result"]
    assert responses[2]["id"] == 2 and not responses[2]["result"]["isError"]
    root = sandbox["roots"][layer.PROJECT]
    assert len(intent_queue.read("interactive", root)) == 1
    assert not skill_store.exists(layer.PROJECT, "date-rule", root)
    hook(sandbox, "Stop")
    assert skill_store.exists(layer.PROJECT, "date-rule", root)
