"""Installer behavior against isolated Codex homes and project directories."""

import json

import pytest

from codex_autoharness import integration


def _write_hooks(root, document):
    path = root / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("project_install", [False, True])
def test_install_is_idempotent_and_uninstall_preserves_existing_hooks(tmp_path, project_install):
    home = tmp_path / "home"
    project = tmp_path / "project" if project_install else None
    target = project or home
    original = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo existing-start"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo existing-stop"}]}],
        },
    }
    hooks = _write_hooks(target, original)
    manual = target / ".agents" / "skills" / "manual-skill" / "SKILL.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("Manually authored instructions.\n", encoding="utf-8")

    integration.install(project=project, home=home)
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    installed_text = json.dumps(installed)
    assert "codex_autoharness" in installed_text or "codex-autoharness" in installed_text
    for event, entries in original["hooks"].items():
        assert entries[0] in installed["hooks"][event]
    learned = target / ".agents" / "skills" / "codex-learn" / "SKILL.md"
    assert learned.is_file()

    integration.install(project=project, home=home)
    assert json.loads(hooks.read_text(encoding="utf-8")) == installed

    integration.uninstall(project=project, home=home)
    assert json.loads(hooks.read_text(encoding="utf-8")) == original
    assert not learned.exists()
    assert manual.read_text(encoding="utf-8") == "Manually authored instructions.\n"
    integration.uninstall(project=project, home=home)
    assert json.loads(hooks.read_text(encoding="utf-8")) == original
    if project_install:
        assert not (home / ".codex").exists()
        assert not (home / ".agents").exists()


def test_install_refuses_to_replace_a_manual_codex_learn_skill(tmp_path):
    home = tmp_path / "home"
    manual = home / ".agents" / "skills" / "codex-learn" / "SKILL.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("My personal codex-learn skill.\n", encoding="utf-8")
    hooks = _write_hooks(home, {"hooks": {}})
    before = hooks.read_bytes()

    with pytest.raises(ValueError):
        integration.install(home=home)

    assert manual.read_text(encoding="utf-8") == "My personal codex-learn skill.\n"
    assert hooks.read_bytes() == before


@pytest.mark.parametrize("malformed", ['{ "hooks": ', '{"hooks": []}', '{"hooks": {"Stop": "bad"}}'])
def test_install_refuses_malformed_hooks_without_replacing_them(tmp_path, malformed):
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(malformed, encoding="utf-8")

    with pytest.raises(ValueError):
        integration.install(home=home)

    assert hooks.read_text(encoding="utf-8") == malformed
    assert not (home / ".agents" / "skills" / "codex-learn").exists()


def test_status_and_doctor_can_inspect_an_empty_home_without_writes(tmp_path):
    home = tmp_path / "home"
    assert isinstance(integration.status(home=home), dict)
    assert isinstance(integration.doctor(home=home), dict)
    assert not home.exists()


def test_status_counts_home_project_as_one_global_layer(tmp_path):
    from codex_autoharness.lib import sidecar, skill_store
    home = tmp_path / "home"
    root = home / ".agents"
    body = "---\nname: shared\ndescription: Use when checking shared state.\n---\nInspect the shared state.\n"
    skill_store.write_body("global", "shared", body, root)
    sidecar.create("global", "shared", 0, root)
    result = integration.status(project=home, home=home)
    assert list(result["layers"]) == ["global"]
    assert result["layers"]["global"]["managed_skills"] == ["shared"]
    assert result["layer_aliases"] == {"project": "global"}
    assert list(result["metrics"]) == ["global"]
    assert result["metrics"]["global"]["live_symbols"] == 1
    assert result["metrics"]["global"]["use_total"] == 0


def test_uninstall_preserves_user_edits_and_hooks_added_after_install(tmp_path):
    home = tmp_path / "home"
    integration.install(home=home)
    hooks = home / ".codex" / "hooks.json"
    document = json.loads(hooks.read_text(encoding="utf-8"))
    added = {"hooks": [{"type": "command", "command": "echo added-later"}]}
    document["hooks"]["Stop"].append(added)
    hooks.write_text(json.dumps(document), encoding="utf-8")
    helper = home / ".agents" / "skills" / "codex-learn" / "SKILL.md"
    helper.write_text("My edited learning workflow.\n", encoding="utf-8")

    integration.uninstall(home=home)

    assert json.loads(hooks.read_text(encoding="utf-8")) == {"hooks": {"Stop": [added]}}
    assert helper.read_text(encoding="utf-8") == "My edited learning workflow.\n"
