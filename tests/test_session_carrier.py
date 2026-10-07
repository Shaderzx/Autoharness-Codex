import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autoharness import cli, config
from codex_autoharness.hook import session_carrier, spawn
from codex_autoharness.lib import layer, ledger, skill_store

LEARNER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    return {"project": tmp_path / "p", "global": tmp_path / "g"}, source


def rollout(home, text="previous redacted learner bundle", identity=LEARNER_ID):
    timestamp = "2026-10-07T01:02:03Z"
    path = Path(home) / f"sessions/2026/10/07/rollout-2026-10-07T01-02-03-{identity}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "session_meta", "payload": {"id": identity, "timestamp": timestamp}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": text}]}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def child(calls, *, output='{"intents":[]}'):
    def invoke(argv, env, bundle):
        calls.append((list(argv), dict(env), bundle))
        rollout(env["CODEX_HOME"])
        Path(argv[argv.index("--output-last-message") + 1]).write_text(output)
        return SimpleNamespace(returncode=0)
    return invoke


def cached(roots):
    return list((layer.state_dir("project", roots["project"]) / "learner-sessions").glob("*.json"))


@pytest.mark.parametrize("carrier", ["resume", "fork"])
def test_reuses_only_owned_learner_and_retains_isolation(setup, carrier):
    roots, source = setup
    rollout(source, "UNREDACTED_PARENT_SESSION", identity="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    calls = []
    for number in range(2):
        spawn.run("current evidence", f"run-{number}", roots=roots, session_id="host-session",
                  carrier=carrier, spawn_fn=child(calls), model="trigger-model", reasoning_effort="high")
    first, second = (item[0] for item in calls)
    assert carrier not in first and "--ephemeral" not in first
    assert second[-3:] == [carrier, LEARNER_ID, "-"]
    assert "--last" not in second and "--ephemeral" not in second
    assert second.index("--sandbox") < second.index(carrier)
    assert second[second.index("--sandbox") + 1] == "read-only"
    assert second[second.index("--model") + 1] == "trigger-model"
    for argv, env, bundle in calls:
        assert "UNREDACTED_PARENT_SESSION" not in bundle
        assert env[config.CHILD_SESSION_ENV] == "1"
        assert all(feature in argv for feature in spawn._DISABLED_FEATURES)
        assert not Path(env["CODEX_HOME"]).exists()
    saved = cached(roots)
    assert len(saved) == 1
    assert saved[0].stat().st_mode & 0o777 == 0o600
    assert saved[0].parent.stat().st_mode & 0o777 == 0o700


def test_no_host_identity_and_curator_keep_fresh_default(setup, monkeypatch):
    roots, _ = setup
    monkeypatch.setattr(config, "REFLECTOR_CARRIER", "resume")
    calls = []
    spawn.run("episode", "no-session", roots=roots, spawn_fn=child(calls))
    spawn.run_curator("curator", roots=roots, spawn_fn=child(calls))
    assert all("--ephemeral" in argv for argv, _, _ in calls)
    assert cached(roots) == []
    assert cli.parser().parse_args(["learn", "--transcript", "example.jsonl"]).session_id is None


def test_carrier_environment_is_operator_opt_in(monkeypatch):
    monkeypatch.setenv("CODEX_AUTOHARNESS_REFLECTOR_CARRIER", " FORK ")
    importlib.reload(config)
    try:
        assert config.REFLECTOR_CARRIER == "fork"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_distinct_session_and_routing_configs_never_share_history(setup):
    roots, source = setup
    calls = []
    settings = [dict(session_id="one", model="m1", reasoning_effort="high"),
                dict(session_id="two", model="m1", reasoning_effort="high"),
                dict(session_id="one", model="m2", reasoning_effort="high"),
                dict(session_id="one", model="m1", reasoning_effort="low")]
    for number, setting in enumerate(settings):
        spawn.run("episode", f"identity-{number}", roots=roots, carrier="resume",
                  spawn_fn=child(calls), **setting)
    assert all("resume" not in argv for argv, _, _ in calls)
    (source / "config.toml").write_text('model="changed-default"\nmodel_provider="openai"\n')
    spawn.run("episode", "source-changed", roots=roots, carrier="resume", session_id="one",
              spawn_fn=child(calls), model="m1", reasoning_effort="high")
    assert "resume" not in calls[-1][0]
    assert len(cached(roots)) == 5


def test_unavailable_resume_retries_fresh_before_any_promotion(setup):
    roots, _ = setup
    spawn.run("episode", "seed", roots=roots, session_id="host", carrier="resume", spawn_fn=child([]))
    calls = []
    row = dict.fromkeys(spawn._INTENT_KEYS)
    row.update(action="create", name="fallback-rule", level="project", reason="Lesson",
               evidence="episode", body="---\nname: fallback-rule\ndescription: Use when testing.\n---\nCheck the operation.\n")
    def failing_resume(argv, env, bundle):
        calls.append(argv)
        if "resume" in argv:
            return SimpleNamespace(returncode=1)
        return child([], output=json.dumps({"intents": [row]}))(argv, env, bundle)
    verdicts = spawn.run("episode", "retry", roots=roots, session_id="host", carrier="resume", spawn_fn=failing_resume)
    assert len(calls) == 2 and "resume" in calls[0] and "resume" not in calls[1]
    assert [verdict["ok"] for verdict in verdicts] == [True]
    assert len(ledger.read("project", "fallback-rule", roots["project"])) == 1


def test_failed_carrier_with_output_never_retries_or_lands(setup):
    roots, _ = setup
    spawn.run("episode", "seed", roots=roots, session_id="host", carrier="fork", spawn_fn=child([]))
    calls = []
    def failing_with_output(argv, env, bundle):
        child(calls)(argv, env, bundle)
        return SimpleNamespace(returncode=1)
    with pytest.raises(spawn.RunnerError, match="child_exit_failure"):
        spawn.run("episode", "failed-output", roots=roots, session_id="host", carrier="fork",
                  spawn_fn=failing_with_output)
    assert len(calls) == 1


def test_prior_learner_messages_cannot_supply_current_evidence(setup):
    roots, _ = setup
    spawn.run("previous quote", "seed", roots=roots, session_id="host", carrier="resume", spawn_fn=child([]))
    row = dict.fromkeys(spawn._INTENT_KEYS)
    row.update(action="create", name="stale-rule", level="project", reason="Lesson",
               evidence="previous quote", body="---\nname: stale-rule\ndescription: Use when testing.\n---\nCheck the operation.\n")
    with pytest.raises(spawn.RunnerError, match="evidence_not_in_source"):
        spawn.run("new unrelated quote", "stale", roots=roots, session_id="host", carrier="resume",
                  spawn_fn=child([], output=json.dumps({"intents": [row]})))
    assert skill_store.read_body("project", "stale-rule", roots["project"]) is None


def test_corrupt_and_oversize_histories_start_fresh(setup):
    roots, _ = setup
    calls = []
    spawn.run("episode", "seed", roots=roots, session_id="host", carrier="fork", spawn_fn=child(calls))
    path = cached(roots)[0]
    malformed_identity = json.dumps({"type": "session_meta", "payload": {"id": 3, "timestamp": 3}})
    for number, invalid in enumerate(("not json", malformed_identity,
                                     "x" * (session_carrier.MAX_HISTORY_BYTES + 1))):
        path.write_text(invalid)
        spawn.run("episode", f"corrupt-{number}", roots=roots, session_id="host", carrier="fork",
                  spawn_fn=child(calls))
        assert "fork" not in calls[-1][0]


def test_history_redaction_preserves_native_json_identity(tmp_path):
    home, root = tmp_path / "home", tmp_path / "p"
    path = rollout(home, "alice@example.com bearer " + "X" * 30 + "\napi_key='test-secret-value'")
    with session_carrier.cache(root, "host", ["model"]) as cache_path:
        session_carrier.save(cache_path, home, root)
        saved = cache_path.read_text()
        assert "alice@example.com" not in saved and "X" * 30 not in saved
        assert "test-secret-value" not in saved
        new_home = tmp_path / "new-home"
        assert session_carrier.restore(cache_path, new_home) == LEARNER_ID
        assert json.loads((new_home / path.relative_to(home)).read_text().splitlines()[0])["payload"]["id"] == LEARNER_ID


def test_cache_discards_persisted_authority_and_paginated_parent_links(tmp_path):
    home, root = tmp_path / "home", tmp_path / "p"
    original = rollout(home)
    rows = [json.loads(line) for line in original.read_text().splitlines()]
    rows[0]["payload"].update(
        history_mode="paginated", history_base={"thread_id": "parent", "end_ordinal_exclusive": 8},
        base_instructions={"text": "PERSISTED_BASE_INSTRUCTIONS"}, dynamic_tools=[{"name": "shell"}],
        selected_capability_roots=[{"path": "/unsafe-tools"}], creator_account_id="private-account",
        multi_agent_version="v1")
    rows.append({"type": "response_item", "payload": {"type": "message", "role": "developer",
                 "content": [{"type": "input_text", "text": "PERSISTED_DEVELOPER_INSTRUCTIONS"}]}})
    rows.append({"type": "turn_context", "payload": {"sandbox_policy": "danger-full-access"}})
    original.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with session_carrier.cache(root, "host", []) as path:
        session_carrier.save(path, home, root)
        saved = path.read_text()
        assert "PERSISTED" not in saved and "private-account" not in saved
        assert "history_base" not in saved and "dynamic_tools" not in saved
        assert "selected_capability_roots" not in saved and "turn_context" not in saved
        metadata = json.loads(saved.splitlines()[0])["payload"]
        assert metadata["history_mode"] == "legacy" and metadata["multi_agent_version"] == "disabled"


def test_cache_retention_and_history_cap_are_bounded(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "p"
    rollout(home)
    for number in range(8):
        with session_carrier.cache(root, str(number), []) as path:
            session_carrier.save(path, home, root)
    assert len(cached({"project": root})) == session_carrier.MAX_CACHED_SESSIONS
    monkeypatch.setattr(session_carrier, "MAX_HISTORY_BYTES", 10)
    with session_carrier.cache(root, "7", []) as path:
        session_carrier.save(path, home, root)
        assert not path.exists()


def test_redirected_cache_directory_is_refused(setup, tmp_path):
    roots, _ = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    state = layer.state_dir("project", roots["project"])
    state.mkdir(parents=True)
    (state / "learner-sessions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(spawn.RunnerError, match="runner_io_or_config_error"):
        spawn.run("episode", "unsafe-cache", roots=roots, session_id="host", carrier="resume",
                  spawn_fn=child([]))
    assert list(outside.iterdir()) == []
