"""Native hook execution through Codex, using a local deterministic provider.

Trust is granted only to temporary fixture sources through the same app-server
config API used by /hooks. No bypass flags or real user config are involved.
"""
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from codex_autoharness import integration
from codex_autoharness.lib import counters, layer, sidecar, skill_store


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_native_engine_runs_reviewed_hook_source(tmp_path):
    fixture_home, work = tmp_path / "fixture-home", tmp_path / "work"
    fixture_home.mkdir()
    work.mkdir()
    installed = integration.install(home=fixture_home)
    hook_path = Path(installed["hooks"])
    hook_log = tmp_path / "events.jsonl"
    recorder = tmp_path / "record_hook.py"
    recorder.write_text(
        "import json, pathlib, subprocess, sys\n"
        "event = sys.stdin.read()\n"
        f"with pathlib.Path({str(hook_log)!r}).open('a') as f: f.write(event.strip() + '\\n')\n"
        "result = subprocess.run(sys.argv[1], shell=True, input=event, text=True, capture_output=True)\n"
        "sys.stdout.write(result.stdout)\nsys.stderr.write(result.stderr)\nraise SystemExit(result.returncode)\n"
    )
    hooks = json.loads(hook_path.read_text())
    for groups in hooks["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = shlex.join([sys.executable, str(recorder), hook["command"]])
    hook_path.write_text(json.dumps(hooks))
    roots = integration.roots(project=work, home=fixture_home)
    description = 'Use when dates fail: "ISO" output.'
    skill_store.write_body(layer.PROJECT, "native-reader",
                           f"---\nname: native-reader\ndescription: {json.dumps(description)}\n---\nUse the existing parser.\n",
                           roots[layer.PROJECT])
    sidecar.create(layer.PROJECT, "native-reader", 0, roots[layer.PROJECT])
    skill = layer.symbol_dir(layer.PROJECT, "native-reader", roots[layer.PROJECT]) / "SKILL.md"
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"probe-model","object":"model","created":0,"owned_by":"local"}]}')

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if sum(previous.get("model") == request.get("model") for previous in requests) == 1:
                names = [tool.get("name") for tool in request.get("tools", [])]
                tool = "exec_command" if "exec_command" in names else "shell_command"
                arguments = {"cmd" if tool == "exec_command" else "command": "cat " + shlex.quote(str(skill))}
                item = {"type": "function_call", "name": tool, "call_id": f"fixture-read-{len(requests)}", "arguments": json.dumps(arguments)}
            else:
                item = {"type": "message", "role": "assistant", "id": "fixture-answer",
                        "content": [{"type": "output_text", "text": "Native hook fixture complete."}]}
            events = [{"type": "response.created", "response": {"id": f"r{len(requests)}"}},
                      {"type": "response.output_item.done", "item": item},
                      {"type": "response.completed", "response": {"id": f"r{len(requests)}",
                       "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    codex_directory = fixture_home / ".codex"
    (codex_directory / "config.toml").write_text(
        'model="probe-model"\nmodel_provider="capture"\napproval_policy="never"\nsandbox_mode="read-only"\n'
        'web_search="disabled"\n[features]\nhooks=true\nplugins=false\ncode_mode_host=false\n'
        'skip_host_skill_discovery=true\nmulti_agent=false\ncomputer_use=false\nbrowser_use=false\n'
        '[model_providers.capture]\nname="Local fixture"\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\nwire_api="responses"\n'
        'request_max_retries=0\nstream_max_retries=0\n'
    )
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_directory)
    env["CODEX_AUTOHARNESS_REFLECT_EVERY_N"] = "10000"
    env["CODEX_AUTOHARNESS_CONSOLIDATE_EVERY_N"] = "1"
    env["CODEX_AUTOHARNESS_NOTIFY"] = "off"
    child_done = tmp_path / "child-finished"
    fake_child = tmp_path / "fake-proposer"
    fake_child.write_text(
        f"#!{sys.executable}\nimport json, os, pathlib, sys, tomllib\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{\"intents\":[]}')\n"
        "cfg = tomllib.loads((pathlib.Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())\n"
        "model = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else cfg.get('model')\n"
        "entry = {'model': model, 'provider': cfg.get('model_provider'), 'run_id': os.environ['CODEX_AUTOHARNESS_RUN_ID']}\n"
        f"with pathlib.Path({str(child_done)!r}).open('a') as f: f.write(json.dumps(entry) + '\\n')\n"
    )
    fake_child.chmod(0o700)
    env["CODEX_AUTOHARNESS_CODEX_BIN"] = str(fake_child)
    for key in ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_AUTOHARNESS_CHILD_SESSION"):
        env.pop(key, None)
    process = subprocess.Popen(["codex", "app-server", "--stdio"], cwd=work, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    messages = queue.Queue()

    def receive():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    notifications = []

    def rpc(number, method, params):
        process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if message.get("id") == number:
                assert "error" not in message, message.get("error")
                return message["result"]
            notifications.append(message)
        raise AssertionError(f"No response to {method}")

    try:
        initialized = rpc(1, "initialize", {"clientInfo": {"name": "autoharness-native-fixture", "version": "0.1"},
                                            "capabilities": {"experimentalApi": True}})
        assert Path(initialized["codexHome"]).resolve() == codex_directory.resolve()
        process.stdin.write('{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        listed = rpc(2, "hooks/list", {"cwds": [str(work)]})["data"][0]["hooks"]
        assert len(listed) == 6 and all(hook["trustStatus"] == "untrusted" for hook in listed)
        assert all(Path(hook["sourcePath"]).resolve() == hook_path.resolve() for hook in listed)
        trust = {hook["key"]: {"trusted_hash": hook["currentHash"]} for hook in listed}
        rpc(3, "config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": trust, "mergeStrategy": "upsert"}],
                                      "reloadUserConfig": True})
        reviewed = rpc(4, "hooks/list", {"cwds": [str(work)]})["data"][0]["hooks"]
        assert all(hook["trustStatus"] == "trusted" for hook in reviewed)
        discovered = rpc(45, "skills/list", {"cwds": [str(work)], "forceReload": True})
        native = [entry for group in discovered["data"] for entry in group["skills"]
                  if entry["name"] == "native-reader"]
        assert len(native) == 1 and native[0]["description"] == description
        started = rpc(5, "thread/start", {"cwd": str(work), "model": "session-model"})
        thread_id = started["thread"]["id"]
        rpc(6, "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Read fixture conventions, then stop."}]})
        deadline = time.monotonic() + 30
        while not any(message.get("method") == "turn/completed" for message in notifications):
            notifications.append(messages.get(timeout=max(0.1, deadline - time.monotonic())))
            assert time.monotonic() < deadline, "Native turn did not complete"
        notifications.clear()
        rpc(65, "turn/start", {"threadId": thread_id, "model": "switched-model", "effort": "high",
                                "input": [{"type": "text", "text": "Use the newly selected model and reread conventions."}]})
        deadline = time.monotonic() + 30
        while not any(message.get("method") == "turn/completed" for message in notifications):
            notifications.append(messages.get(timeout=max(0.1, deadline - time.monotonic())))
            assert time.monotonic() < deadline, "Switched-model turn did not complete"
        rpc(7, "thread/archive", {"threadId": thread_id})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in hook_log.read_text().splitlines()] if hook_log.exists() else []
            if any(event["hook_event_name"] == "SessionEnd" for event in events):
                break
            time.sleep(0.05)
        names = [event["hook_event_name"] for event in events]
        assert set(names) == set(integration.EVENTS), names
        assert counters.request_count(layer.PROJECT, roots[layer.PROJECT]) == 2
        assert sidecar.read(layer.PROJECT, "native-reader", roots[layer.PROJECT])["use"] == 2
        assert [event["model"] for event in events if event["hook_event_name"] == "Stop"] == ["session-model", "switched-model"]
        assert all("model" not in event for event in events if event["hook_event_name"] == "SessionEnd")
        post = next(event for event in events if event["hook_event_name"] == "PostToolUse")
        assert post["tool_name"] == "Bash"
        assert "command" in post["tool_input"] and "tool_response" in post
        assert all(event.get("transcript_path") for event in events)
        assert "Codex AutoHarness learned skills" in json.dumps(requests[0])
        deadline = time.monotonic() + 5
        children = []
        while len(children) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
            children = [json.loads(line) for line in child_done.read_text().splitlines()] if child_done.exists() else []
        assert len(children) == 3, "Both curations and the SessionEnd learner must run"
        assert sorted(child["model"] for child in children) == ["session-model", "switched-model", "switched-model"]
        assert all(child["provider"] == "capture" for child in children)
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdin.close()
        reader.join(timeout=3)
        process.stdout.close()
        server.shutdown()
        server.server_close()
