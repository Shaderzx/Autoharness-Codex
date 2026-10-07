import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autoharness import config
from codex_autoharness.hook import spawn
from codex_autoharness.lib import (
    counters,
    intent_queue,
    layer,
    ledger,
    sidecar,
    skill_store,
)

GOOD = "---\nname: {n}\ndescription: Use when testing a specific operation.\ncategory: testing\n---\nRun the operation against a temporary fixture.\n"


@pytest.fixture(autouse=True)
def isolated_source_home(tmp_path, monkeypatch):
    directory = tmp_path / "original-codex-home"
    directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(directory))
    return directory


def _roots(tmp_path):
    return {"global": tmp_path / "g", "project": tmp_path / "p"}


def proposal(**values):
    row = dict.fromkeys(spawn._INTENT_KEYS)
    row.update(action="create", name="learned", level="project", body=GOOD.format(n="learned"),
               reason="Repeated operation", evidence="Use a temporary fixture")
    row.update(values)
    return row


def fake_child(rows, *, returncode=0, observe=None):
    def run(argv, env, bundle):
        if observe:
            observe(argv, env, bundle)
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({"intents": rows}))
        return SimpleNamespace(returncode=returncode)
    return run


def test_provider_auth_copied_privately_without_extensions(tmp_path, isolated_source_home):
    """Provider and private auth transfer must exclude executable integrations."""
    original = '''model = "codex-model"
model_provider = "custom"
approval_policy = "never"
[model_providers.custom]
name = "custom"
base_url = "https://example.invalid/v1"
env_key = "EXAMPLE_API_KEY"
http_headers = {"Authorization" = "secret-example"}
[mcp_servers.danger]
command = "must-not-run"
[features]
hooks = true
plugins = true
'''
    (isolated_source_home / "config.toml").write_text(original)
    (isolated_source_home / "auth.json").write_text('{"OPENAI_API_KEY":"fake-test-key"}')
    (isolated_source_home / "hooks.json").write_text('{}')
    env = {"CODEX_HOME": str(isolated_source_home), "PATH": os.environ["PATH"], "MCP_TOKEN": "unused"}
    target = spawn._isolated_home(tmp_path, env)
    copied = tomllib.loads((target / "config.toml").read_text())
    assert copied["model"] == "codex-model"
    assert copied["model_providers"]["custom"]["http_headers"] == {"Authorization": "secret-example"}
    assert "mcp_servers" not in copied and "features" not in copied
    assert not (target / "hooks.json").exists()
    assert copied["cli_auth_credentials_store"] == "file"
    with spawn.auth.isolated_credentials(isolated_source_home, target):
        assert (target / "auth.json").read_bytes() == (isolated_source_home / "auth.json").read_bytes()
        assert (target / "auth.json").stat().st_mode & 0o777 == 0o600
    assert "MCP_TOKEN" not in env
    assert (isolated_source_home / "config.toml").read_text() == original

    # A triggering session can select a different provider and model default
    # effort without copying a shared profile's provider or low-effort setting.
    switched = tmp_path / "switched"
    switched.mkdir()
    target = spawn._isolated_home(switched, {"CODEX_HOME": str(isolated_source_home)},
                                  model_provider="openai", reasoning_effort="")
    switched_config = tomllib.loads((target / "config.toml").read_text())
    assert switched_config["model_provider"] == "openai"
    assert "model_reasoning_effort" not in switched_config


def test_bundle_only_contains_managed_bodies_and_redacts(tmp_path):
    roots = _roots(tmp_path)
    skill_store.write_body("project", "managed", GOOD.format(n="managed") + "MANAGED_BODY", roots["project"])
    sidecar.create("project", "managed", 0, roots["project"])
    skill_store.write_body("project", "external", GOOD.format(n="external") + "EXTERNAL_BODY", roots["project"])
    seen = {}
    def observe(argv, env, bundle):
        seen.update(bundle=bundle, env=dict(env), home=env["CODEX_HOME"])
        assert env[config.CHILD_SESSION_ENV] == "1"
        assert env[config.RUN_ID_ENV] == "run-bundle"
    spawn.run("Use a temporary fixture", "run-bundle", roots=roots,
              spawn_fn=fake_child([], observe=observe))
    assert "MANAGED_BODY" in seen["bundle"] and "EXTERNAL_BODY" not in seen["bundle"]
    assert "external/read-only" in seen["bundle"]
    assert not Path(seen["home"]).exists()


@pytest.mark.parametrize("text", [
    '{}', '{"intents":[],"extra":true}', '{"intents":[],"intents":[]}',
    '{"intents":[{}]}', '{"intents":[NaN]}', '{"intents":', '```json\n{"intents":[]}\n```',
])
def test_invalid_output_is_rejected(text):
    with pytest.raises(spawn.RunnerError):
        spawn.parse_proposals(text)


def test_later_malformed_intent_does_not_partially_land(tmp_path):
    roots = _roots(tmp_path)
    with pytest.raises(spawn.RunnerError, match="invalid_proposal_schema"):
        spawn.run("Use a temporary fixture", "bad-schema", roots=roots,
                  spawn_fn=fake_child([proposal(), {"action": "delete"}]))
    assert skill_store.read_body("project", "learned", roots["project"]) is None
    assert intent_queue.read("bad-schema", roots["project"]) == []


def test_failed_child_with_valid_output_never_lands(tmp_path):
    roots = _roots(tmp_path)
    with pytest.raises(spawn.RunnerError, match="child_exit_failure"):
        spawn.run("Use a temporary fixture", "bad-exit", roots=roots,
                  spawn_fn=fake_child([proposal()], returncode=1))
    assert skill_store.read_body("project", "learned", roots["project"]) is None
    account = json.loads((layer.state_dir("project", roots["project"]) / "runs/bad-exit.json").read_text())
    assert account["status"] == "error" and account["error"] == "child_exit_failure"


@pytest.mark.parametrize("mode,error", [("timeout", "timeout"), ("failure", "child_exit_failure"), ("success", None)])
def test_timeout_records_only_safe_code(tmp_path, mode, error):
    roots = _roots(tmp_path)
    original_handler = signal.getsignal(signal.SIGTERM)
    pids = tmp_path / "pids"
    homes = []
    code = """import os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}")
if sys.argv[2] == "timeout":
    time.sleep(30)  # secret-in-command must not enter the run account
pathlib.Path(sys.argv[3]).write_text('{"intents":[]}')
sys.exit(1 if sys.argv[2] == "failure" else 0)
"""
    def timeout(argv, env, bundle):
        homes.append(Path(env["CODEX_HOME"]))
        output = argv[argv.index("--output-last-message") + 1]
        return spawn._detached_spawn([sys.executable, "-c", code, str(pids), mode, output], env, bundle, timeout_s=1)
    try:
        if error:
            with pytest.raises(spawn.RunnerError, match=error):
                spawn.run("window", "timeout-run", roots=roots, spawn_fn=timeout)
        else:
            spawn.run("window", "timeout-run", roots=roots, spawn_fn=timeout)
        account = (layer.state_dir("project", roots["project"]) / "runs/timeout-run.json").read_text()
        assert "secret" not in account and json.loads(account).get("error") == error
        assert homes and not homes[0].exists()
        assert signal.getsignal(signal.SIGTERM) == original_handler
        for pid in map(int, pids.read_text().split()):
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
            assert not status or status.startswith("Z"), f"proposer descendant {pid} survived: {status}"
    finally:
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_terminated_worker_cleans_its_proposer(tmp_path):
    marker = tmp_path / "proposer-pid"
    code = ("import json,os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("
            "json.dumps({'pid':os.getpid(), 'home':os.environ['CODEX_HOME']})); time.sleep(30)")
    worker_code = f"""import sys
from pathlib import Path
sys.path.insert(0, {str(Path(spawn.__file__).resolve().parents[2])!r})
from codex_autoharness.hook import spawn
def propose(argv, env, bundle):
    return spawn._detached_spawn([sys.executable, '-c', {code!r}, {str(marker)!r}], env, bundle, timeout_s=30)
spawn.run('window', 'terminated-run', roots={{'project':Path({str(tmp_path / 'project')!r}),
          'global':Path({str(tmp_path / 'global')!r})}}, spawn_fn=propose,
          source_home={str(tmp_path / 'original-codex-home')!r})
"""
    with subprocess.Popen([sys.executable, "-c", worker_code], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as worker:
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), "proposer did not start"
            info = json.loads(marker.read_text())
            worker.terminate()
            worker.wait(timeout=5)
            assert worker.returncode == 128 + signal.SIGTERM
            assert not Path(info["home"]).exists()
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(info["pid"])], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
            assert not status or status.startswith("Z"), f"proposer survived worker termination: {status}"
        finally:
            worker.kill()
            if marker.exists():
                try:
                    os.kill(json.loads(marker.read_text())["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_schema_normalizes_subfiles_and_rejects_duplicates():
    row = proposal(files=[{"path": "references/test.md", "content": "Details"}])
    assert spawn.parse_proposals(json.dumps({"intents": [row]}))[0]["files"] == {"references/test.md": "Details"}
    row["files"] *= 2
    with pytest.raises(spawn.RunnerError):
        spawn.parse_proposals(json.dumps({"intents": [row]}))


def test_curator_merges_managed_and_preserves_native(tmp_path):
    roots = _roots(tmp_path)
    for name in ("umbrella", "narrow", "native"):
        skill_store.write_body("project", name, GOOD.format(n=name), roots["project"])
        if name != "native":
            sidecar.create("project", name, 0, roots["project"])
    rows = [proposal(action="patch", name="umbrella", level=None, body=None,
                     old_string="Run the operation", new_string="Run and check the operation",
                     evidence="Run the operation against a temporary fixture."),
            proposal(action="delete", name="narrow", level=None, body=None, absorbed_into="umbrella",
                     evidence="Run the operation against a temporary fixture."),
            proposal(action="delete", name="native", level=None, body=None,
                     evidence="Run the operation against a temporary fixture.")]
    verdicts = spawn.run_curator("curator-run", roots=roots, spawn_fn=fake_child(rows))
    assert [v["ok"] for v in verdicts] == [True, True, False]
    assert skill_store.read_body("project", "native", roots["project"])
    assert skill_store.read_body("project", "narrow", roots["project"]) is None
    assert "Run and check the operation" in skill_store.read_body("project", "umbrella", roots["project"])
    retired = ledger.read("project", "narrow", roots["project"], archived=True)[-1]
    assert retired["action"] == "delete" and retired["absorbed_into"] == "umbrella"
    archived = layer.archive_dir("project", roots["project"]) / "narrow"
    assert (archived / retired["evidence"]).read_text() == "Run the operation against a temporary fixture."
    snapshot = layer.state_dir("project", roots["project"]) / "snapshots/curator-run-project.tar.gz"
    with tarfile.open(snapshot) as archive:
        names = archive.getnames()
    assert any("umbrella" in p for p in names) and not any("native" in p for p in names)


def test_snapshots_keep_at_most_five_and_failure_aborts(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    skill_store.write_body("project", "managed", GOOD.format(n="managed"), roots["project"])
    sidecar.create("project", "managed", 0, roots["project"])
    monkeypatch.setattr(config, "SNAPSHOT_KEEP", 99)
    for i in range(7):
        spawn._snapshot_skills(f"snapshot-{i}", roots)
    assert len(list((layer.state_dir("project", roots["project"]) / "snapshots").glob("*.tar.gz"))) == 5
    called = []
    monkeypatch.setattr(spawn, "_snapshot_skills", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(spawn.RunnerError, match="snapshot_or_bundle_error"):
        spawn.run_curator("no-snapshot", roots=roots, spawn_fn=lambda *a: called.append(True))
    assert called == []


def test_main_advances_offset_only_after_success(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    counters.write_session_offset("session", 7, roots["project"])
    argv = [str(transcript), "session", "run-offset", str(roots["project"]), str(roots["global"])]
    with pytest.raises(spawn.RunnerError, match="capture_error"):
        spawn.main(argv)
    account = json.loads((layer.state_dir("project", roots["project"]) / "runs/run-offset.json").read_text())
    assert account["error"] == "capture_error" and counters.session_offset("session", roots["project"]) == 7
    transcript.touch()  # an existing empty source is a legitimate no-op
    assert spawn.main(argv) == [] and counters.session_offset("session", roots["project"]) == 7
    monkeypatch.setattr(spawn.capture, "window", lambda *a: ("window", 42))
    monkeypatch.setattr(spawn.capture, "digest", lambda *a: "digest")
    monkeypatch.setattr(spawn, "run", lambda *a, **k: (_ for _ in ()).throw(spawn.RunnerError("timeout")))
    with pytest.raises(spawn.RunnerError):
        spawn.main(argv)
    assert counters.session_offset("session", roots["project"]) == 7
    monkeypatch.setattr(spawn, "run", lambda *a, **k: [])
    spawn.main(argv)
    assert counters.session_offset("session", roots["project"]) == 42


def test_fabricated_evidence_never_lands(tmp_path):
    roots = _roots(tmp_path)
    with pytest.raises(spawn.RunnerError, match="evidence_not_in_source"):
        spawn.run("Unrelated episode", "invented-evidence", roots=roots,
                  spawn_fn=fake_child([proposal()]))
    assert skill_store.read_body("project", "learned", roots["project"]) is None
    assert intent_queue.read("invented-evidence", roots["project"]) == []


def test_redaction_precedes_window_and_digest_clipping(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(config, "CAPTURE_MAX_WINDOW_BYTES", 100)
    monkeypatch.setattr(config, "DIGEST_MAX_BYTES", 100)
    secret = "-----BEGIN PRIVATE KEY-----\n" + "PRIVATE_MATERIAL" * 200 + "\n-----END PRIVATE KEY-----"
    seen = []
    spawn.run(secret, "redaction-first", roots=roots, digest=secret,
              spawn_fn=fake_child([], observe=lambda argv, env, bundle: seen.append(bundle)))
    assert "PRIVATE_MATERIAL" not in seen[0]
    assert "[REDACTED:" in seen[0]


def test_shared_home_root_is_indexed_and_snapshotted_once(tmp_path):
    roots = {"project": tmp_path / "shared", "global": tmp_path / "shared"}
    skill_store.write_body("global", "managed", GOOD.format(n="managed"), roots["global"])
    sidecar.create("global", "managed", 0, roots["global"])
    assert spawn.description_index(roots).count("- managed") == 1
    spawn._snapshot_skills("same-root", roots)
    snapshots = list((layer.state_dir("project", roots["project"]) / "snapshots").glob("*.tar.gz"))
    assert len(snapshots) == 1 and snapshots[0].name.endswith("-global.tar.gz")


@pytest.mark.parametrize("changed", ["body", "support-file"])
def test_concurrent_authored_edit_aborts_entire_run(tmp_path, changed):
    roots = _roots(tmp_path)
    root = roots["project"]
    skill_store.write_body("project", "managed", GOOD.format(n="managed"), root)
    sidecar.create("project", "managed", 0, root)
    support = layer.subfile_path("project", "managed", "references/notes.md", root)
    support.parent.mkdir(parents=True, exist_ok=True)
    support.write_text("Original supporting detail")
    original = skill_store.read_body("project", "managed", root)
    def concurrent_edit(*args):
        if changed == "body":
            skill_store.write_body("project", "managed", original + "A concurrent improvement.\n", root)
        else:
            support.write_text("A concurrent supporting improvement")
    rows = [proposal(action="update", name="managed", body=GOOD.format(n="managed"), level=None)]
    with pytest.raises(spawn.RunnerError, match="stale_library"):
        spawn.run("Use a temporary fixture", "stale-run", roots=roots,
                  spawn_fn=fake_child(rows, observe=concurrent_edit))
    assert intent_queue.read("stale-run", root) == []
    if changed == "body":
        assert "concurrent improvement" in skill_store.read_body("project", "managed", root)
    else:
        assert "concurrent supporting" in support.read_text()


def test_usage_and_evidence_changes_do_not_invalidate_proposal(tmp_path):
    roots = _roots(tmp_path)
    root = roots["project"]
    skill_store.write_body("project", "managed", GOOD.format(n="managed"), root)
    sidecar.create("project", "managed", 0, root)
    def use(*args):
        sidecar.bump_use("project", "managed", root)
        evidence = layer.subfile_path("project", "managed", "references/evidence-abc123.md", root)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("New provenance")
    rows = [proposal(action="patch", name="managed", level=None, body=None,
                     old_string="Run the operation", new_string="Check the operation")]
    result = spawn.run("Use a temporary fixture", "usage-run", roots=roots,
                       spawn_fn=fake_child(rows, observe=use))
    assert [row["ok"] for row in result] == [True]


def test_changed_absorption_target_prevents_retiring_source(tmp_path):
    roots = _roots(tmp_path)
    root = roots["project"]
    for name in ("umbrella", "narrow"):
        skill_store.write_body("project", name, GOOD.format(n=name), root)
        sidecar.create("project", name, 0, root)
    def concurrent_edit(*args):
        skill_store.write_body("project", "umbrella", GOOD.format(n="umbrella") + "New concurrent rule.\n", root)
    row = proposal(action="delete", name="narrow", level=None, body=None, absorbed_into="umbrella",
                   evidence="Run the operation against a temporary fixture.")
    with pytest.raises(spawn.RunnerError, match="stale_library"):
        spawn.run_curator("stale-umbrella", roots=roots, spawn_fn=fake_child([row], observe=concurrent_edit))
    assert skill_store.read_body("project", "narrow", root) is not None
