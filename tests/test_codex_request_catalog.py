"""Verify the installed Codex actually advertises no proposer tools.

This uses a loopback provider and no credentials or model inference. It catches
feature flags that parse successfully but still expose a dangerous tool.
"""
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from codex_autoharness.hook import session_carrier
from codex_autoharness.hook.spawn import build_command


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_real_codex_proposer_request_has_no_tools(tmp_path):
    """Verify native Codex sends the selected model and effort with no tools."""
    captured = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Silence HTTP fixture access logs."""
            pass

        def do_GET(self):
            """Serve the local provider model-list fixture."""
            data = json.dumps({"data": [{"id": "probe-model", "object": "model",
                                         "created": 0, "owned_by": "local"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            """Capture a native provider request and return the probe response."""
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(payload)
            received.set()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local probe complete","type":"invalid_request_error"}}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    codex_directory, work = tmp_path / "codex-home", tmp_path / "work"
    codex_directory.mkdir()
    work.mkdir()
    (codex_directory / "config.toml").write_text(
        'model = "probe-model"\nmodel_provider = "capture"\napproval_policy = "never"\n'
        'sandbox_mode = "read-only"\n[model_providers.capture]\nname = "Local verification"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'wire_api = "responses"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
    )
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_directory)
    for key in ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"):
        env.pop(key, None)
    command = build_command(output_path=tmp_path / "output.json", cwd=work,
                            model="session-selected-model", reasoning_effort="high")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, text=True)
    try:
        process.stdin.write('Return {"intents":[]} for this local verification.')
        process.stdin.close()
        assert received.wait(20), "Codex did not reach the local verification provider"
        assert len(captured) == 1
        assert captured[0].get("tools", []) == []
        assert captured[0]["model"] == "session-selected-model"
        assert captured[0].get("reasoning", {}).get("effort") == "high"
    finally:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
@pytest.mark.parametrize("carrier", ["resume", "fork"])
def test_real_codex_reuses_isolated_learner_history(tmp_path, carrier):
    """Verify three native reuse generations retain context without tools or copied state."""
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Silence HTTP fixture access logs."""
            pass

        def do_GET(self):
            """Serve the local provider model-list fixture."""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            """Capture a native provider request and return the probe response."""
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            events = [
                {"type": "response.created", "response": {"id": "local-response"}},
                {"type": "response.output_item.done", "item": {
                    "type": "message", "role": "assistant", "id": "local-message",
                    "content": [{"type": "output_text", "text": '{"intents":[]}'}]}},
                {"type": "response.completed", "response": {"id": "local-response",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
            ]
            data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(data.encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cache_path = tmp_path / "learner.json"
    previous_id = None
    markers = ["isolated-learner-seed", "isolated-learner-second", "isolated-learner-third"]
    try:
        for index, marker in enumerate(markers):
            home, work = tmp_path / f"home-{index}", tmp_path / f"work-{index}"
            home.mkdir()
            work.mkdir()
            model, provider, effort = f"probe-model-{index}", f"capture_{index}", ("high", "low", "high")[index]
            (home / "config.toml").write_text(
                f'model = "{model}"\nmodel_provider = "{provider}"\napproval_policy = "never"\n'
                f'sandbox_mode = "read-only"\n[model_providers.{provider}]\nname = "Local verification"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'wire_api = "responses"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            )
            assert not list(home.glob("*.sqlite*")), "The transfer must work without copied SQLite state"
            learner_id = session_carrier.restore(cache_path, home)
            assert learner_id == previous_id
            output = tmp_path / f"output-{index}.json"
            command = build_command(output_path=output, cwd=work, model=model,
                                    reasoning_effort=effort, carrier=carrier, learner_id=learner_id)
            result = subprocess.run(command, input=marker, text=True, capture_output=True,
                                    env={"PATH": os.environ["PATH"], "CODEX_HOME": str(home)}, timeout=30)
            assert result.returncode == 0, result.stderr
            assert json.loads(output.read_text()) == {"intents": []}
            assert len(captured) == index + 1
            request = captured[-1]
            assert request.get("tools", []) == []
            assert request["model"] == model
            assert request.get("reasoning", {}).get("effort") == effort
            for seen_marker in markers[:index + 1]:
                assert seen_marker in json.dumps(request["input"])
            assert str(work) in json.dumps(request["input"])
            session_carrier.save(cache_path, home, tmp_path)
            assert cache_path.is_file()
            previous_id = json.loads(cache_path.read_text().splitlines()[0])["payload"]["id"]
            shutil.rmtree(home)
            shutil.rmtree(work)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
