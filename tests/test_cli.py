"""User-facing commands use the supplied roots and reject invalid proposals."""

import json
import tarfile

import pytest

from codex_autoharness import cli
from codex_autoharness.hook import spawn
from codex_autoharness.lib import sidecar, skill_store


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
    assert cli.main([*argv, "record-use", "date-format", "--level", level]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded == {"ok": True, "name": "date-format", "level": level}
    metadata = json.loads((skill.parent / ".sidecar.json").read_text())
    assert metadata["use"] == 1 and metadata["view"] == 0
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


@pytest.mark.parametrize("level", ["project", "global"])
def test_restore_snapshot_preserves_files_and_refuses_overwrite(cli_roots, capsys, level):
    argv, home, project, _, _ = cli_roots
    roots = {"project": project / ".agents", "global": home / ".agents"}
    root = roots[level]
    body = "---\nname: example\ndescription: Use when testing.\n---\nOriginal lesson.\n"
    skill_store.write_body(level, "example", body, root)
    sidecar.create(level, "example", anchor=3, root=root)
    directory = root / "skills" / "example"
    script = directory / "scripts" / "check.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    directory.chmod(0o700)
    script.parent.chmod(0o500)
    (directory / "ledger.jsonl").write_text('{"evidence":"original"}\n')
    original = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    spawn._snapshot_skills("before", roots)
    snapshot = roots["project"] / "codex-autoharness" / "snapshots" / f"before-{level}.tar.gz"
    command = [*argv, "restore", "example", "--level", level, "--snapshot", str(snapshot)]
    skill_store.write_body(level, "example", "Newer lesson", root)
    assert cli.main(command) == 1
    assert "archive it first" in capsys.readouterr().out
    assert skill_store.read_body(level, "example", root) == "Newer lesson"
    archived = skill_store.archive(level, "example", root)
    assert cli.main(command) == 0
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == original
    assert script.stat().st_mode & 0o777 == 0o755
    assert directory.stat().st_mode & 0o777 == 0o700
    assert script.parent.stat().st_mode & 0o777 == 0o500
    assert (archived / "SKILL.md").read_text() == "Newer lesson"
    assert snapshot.is_file()


@pytest.mark.parametrize("member_name,member_type", [
    ("skills/example/../../escape", tarfile.REGTYPE),
    ("skills/example/link", tarfile.SYMTYPE),
    ("skills/example/link", tarfile.LNKTYPE),
])
def test_restore_snapshot_rejects_unsafe_members(cli_roots, tmp_path, capsys, member_name, member_type):
    argv, _, project, _, _ = cli_roots
    snapshot = tmp_path / "unsafe.tar.gz"
    with tarfile.open(snapshot, "w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.type = member_type
        member.linkname = str(tmp_path / "escape") if member_type != tarfile.REGTYPE else ""
        archive.addfile(member)
    assert cli.main([*argv, "restore", "example", "--snapshot", str(snapshot)]) == 1
    assert "unsafe snapshot member" in capsys.readouterr().out
    assert not (project / ".agents" / "skills" / "example").exists()
    assert not (tmp_path / "escape").exists()
    assert not list((project / ".agents" / "skills").glob(".snapshot-*"))
