"""User-facing commands use the supplied roots and reject invalid proposals."""

import json

import pytest

from codex_autoharness import cli


@pytest.fixture
def cli_roots(tmp_path, monkeypatch):
    fake_home = tmp_path / "default-home"
    fake_home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CODEX_AUTOHARNESS_GLOBAL_ROOT", raising=False)
    monkeypatch.delenv("CODEX_AUTOHARNESS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(cwd)
    home = tmp_path / "chosen-home"
    project = tmp_path / "chosen-project"
    project.mkdir()
    argv = ["--home", str(home), "--project", str(project)]
    return argv, home, project, fake_home, cwd


def test_help_lists_the_supported_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("install", "uninstall", "status", "doctor", "stage", "learn", "curate", "archive", "restore", "history"):
        assert command in output


def test_status_reports_without_creating_state(cli_roots, capsys):
    argv, home, project, fake_home, cwd = cli_roots
    assert cli.main([*argv, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["installed"] is False
    assert status["layers"]["global"]["skills_dir"] == str(home / ".agents" / "skills")
    assert status["layers"]["project"]["skills_dir"] == str(project / ".agents" / "skills")
    assert not home.exists()
    assert not list(project.iterdir())
    assert not list(fake_home.iterdir())
    assert not list(cwd.iterdir())


@pytest.mark.parametrize("level", ["global", "project"])
def test_stage_valid_proposal_writes_only_to_the_supplied_roots(tmp_path, cli_roots, capsys, level):
    argv, home, project, fake_home, cwd = cli_roots
    proposal = {
        "action": "create",
        "name": "date-format",
        "level": level,
        "body": "---\nname: date-format\ndescription: Use when formatting ISO dates.\ncategory: formatting\n---\n# ISO dates\nUse date.isoformat().\n",
        "reason": "The user repeated this formatting preference.",
        "evidence": "Format dates in ISO 8601 notation.",
    }
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(proposal), encoding="utf-8")

    assert cli.main([*argv, "stage", "--file", str(proposal_file)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    target = home if level == "global" else project
    skill = target / ".agents" / "skills" / "date-format" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == proposal["body"]
    assert (skill.parent / ".sidecar.json").is_file()
    assert not list(fake_home.iterdir())
    assert not list(cwd.iterdir())


@pytest.mark.parametrize("proposal", [
    {"action": "create", "name": "../escape", "body": "unsafe", "reason": "r", "evidence": "e"},
    {"action": "create", "name": "bad-skill", "body": "Missing required frontmatter", "reason": "r", "evidence": "e"},
])
def test_stage_rejects_invalid_proposals_without_writing_skills(tmp_path, cli_roots, proposal):
    argv, home, project, fake_home, cwd = cli_roots
    proposal_file = tmp_path / "invalid.json"
    proposal_file.write_text(json.dumps(proposal), encoding="utf-8")

    assert cli.main([*argv, "stage", "--file", str(proposal_file)]) != 0
    assert not list(home.rglob("SKILL.md"))
    assert not list(project.rglob("SKILL.md"))
    assert not list(fake_home.iterdir())
    assert not list(cwd.iterdir())


@pytest.mark.parametrize("level", ["global", "project"])
def test_archive_preserves_a_user_authored_skill(cli_roots, capsys, level):
    argv, home, project, _, _ = cli_roots
    target = home if level == "global" else project
    skill = target / ".agents" / "skills" / "manual-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("User-authored instructions.\n", encoding="utf-8")

    assert cli.main([*argv, "archive", "manual-skill", "--level", level]) != 0

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert skill.read_text(encoding="utf-8") == "User-authored instructions.\n"
    assert not (target / ".agents" / "skills" / ".archive" / "manual-skill").exists()


def test_stage_reports_malformed_json_without_creating_skills(tmp_path, cli_roots, capsys):
    argv, home, project, _, _ = cli_roots
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"action": ', encoding="utf-8")

    assert cli.main([*argv, "stage", "--file", str(malformed)]) != 0

    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not list(home.rglob("SKILL.md"))
    assert not list(project.rglob("SKILL.md"))
