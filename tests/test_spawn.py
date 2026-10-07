import json
import os
import subprocess
import tarfile
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


def test_timeout_records_only_safe_code(tmp_path):
    roots = _roots(tmp_path)
    def timeout(*args):
        raise subprocess.TimeoutExpired("secret-in-command", 1, stderr="secret-in-stderr")
    with pytest.raises(spawn.RunnerError, match="timeout"):
        spawn.run("window", "timeout-run", roots=roots, spawn_fn=timeout)
    account = (layer.state_dir("project", roots["project"]) / "runs/timeout-run.json").read_text()
    assert "secret" not in account and '"error": "timeout"' in account


def test_schema_normalizes_subfiles_and_rejects_duplicates():
    row = proposal(files=[{"path": "references/test.md", "content": "Details"}])
    assert spawn.parse_proposals(json.dumps({"intents": [row]}))[0]["files"] == {"references/test.md": "Details"}
    row["files"] *= 2
    with pytest.raises(spawn.RunnerError):
        spawn.parse_proposals(json.dumps({"intents": [row]}))


@pytest.mark.parametrize("retire_first", [False, True])
def test_curator_merges_managed_and_preserves_native(tmp_path, retire_first):
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
    if retire_first:
        rows[:2] = rows[:2][::-1]
    verdicts = spawn.run_curator("curator-run", roots=roots, spawn_fn=fake_child(rows))
    assert [v["ok"] for v in verdicts] == [True, True, False]
    assert [v["action"] for v in verdicts] == [row["action"] for row in rows]
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
    monkeypatch.setattr(spawn.capture, "window", lambda *a: ("window", 42))
    monkeypatch.setattr(spawn.capture, "digest", lambda *a: "digest")
    counters.write_session_offset("session", 7, roots["project"])
    argv = ["/transcript.jsonl", "session", "run-offset", str(roots["project"]), str(roots["global"])]
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
