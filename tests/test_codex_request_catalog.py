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

from codex_autoharness.hook.spawn import build_command


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_real_codex_proposer_request_has_no_tools(tmp_path):
    captured = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps({"data": [{"id": "probe-model", "object": "model",
                                         "created": 0, "owned_by": "local"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
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
