import subprocess

import pytest

from codex_autoharness.hook import on_session_start, on_skill_call, promoter
from codex_autoharness.lib import counters, git_exclude, layer, sidecar, skill_store


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def main_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def linked_worktree(main_repo):
    wt = main_repo / ".agents" / "worktrees" / "br"
    _git(main_repo, "worktree", "add", "-q", "-b", "br", str(wt))
    return wt


def _project_root_at(monkeypatch, cwd):
    monkeypatch.chdir(cwd)
    return layer.default_root(layer.PROJECT)


def test_project_root_non_git_is_cwd(tmp_path, monkeypatch):
    assert _project_root_at(monkeypatch, tmp_path) == tmp_path / ".agents"


def test_project_root_repo_subdir_stays_cwd_no_jump_to_repo_root(main_repo, monkeypatch):
    sub = main_repo / "nested_project"
    sub.mkdir()
    assert _project_root_at(monkeypatch, sub) == sub / ".agents"


def test_project_root_linked_worktree_maps_to_main_root(linked_worktree, main_repo, monkeypatch):
    assert _project_root_at(monkeypatch, linked_worktree) == main_repo / ".agents"


def test_managed_skills_stay_out_of_git_diff_without_hiding_user_skills(main_repo, linked_worktree, tmp_path, monkeypatch):
    """Exclude owned runtime files while preserving authored content and cheap counters."""
    root = main_repo / "nested[?]" / ".agents"
    roots = {layer.GLOBAL: tmp_path / "home" / ".agents", layer.PROJECT: root}
    exclude = main_repo / ".git" / "info" / "exclude"
    original = b"# user's local rules\nprivate.txt"  # retain an unterminated final line
    exclude.write_bytes(original)
    user = skill_store.skill_path(layer.PROJECT, "user-skill", root)
    user.parent.mkdir(parents=True)
    user.write_text("User instructions.\n")
    tracked = skill_store.skill_path(layer.PROJECT, "tracked-skill", root)
    tracked.parent.mkdir(parents=True)
    tracked.write_text("Tracked instructions.\n")
    _git(main_repo, "add", str(tracked.relative_to(main_repo)))
    _git(main_repo, "commit", "-q", "-m", "track chosen skill")
    sidecar.create(layer.PROJECT, "tracked-skill", 0, root)
    tracked.write_text("Changed tracked instructions.\n")
    new_guide = tracked.parent / "references" / "new-guide.md"
    new_guide.parent.mkdir()
    new_guide.write_text("A new user-authored supporting file.\n")
    intent = {"action": "create", "name": "learned", "level": layer.PROJECT,
              "body": "---\nname: learned\ndescription: Use when formatting dates.\n---\nUse strftime.\n",
              "reason": "Repeated formatting", "evidence": "Format a date."}
    assert promoter.promote(intent, roots=roots)["ok"]
    learned = skill_store.skill_path(layer.PROJECT, "learned", root)
    on_skill_call.on_skill_read({"tool_name": "Read", "tool_input": {"file_path": str(learned)},
                               "tool_response": {"exit_code": 0}}, roots=roots)
    assert sidecar.read(layer.PROJECT, "learned", root)["use"] == 1

    def status(cwd=main_repo):
        """Report tracked changes and every untracked file in the selected worktree."""
        return subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"],
                                       cwd=cwd, text=True)

    changed = status()
    assert "learned" not in changed and "codex-autoharness" not in changed, changed
    assert "user-skill/SKILL.md" in changed and "tracked-skill/SKILL.md" in changed
    assert "tracked-skill/references/new-guide.md" in changed
    assert exclude.read_bytes().startswith(original + b"\n")
    before = exclude.stat().st_mtime_ns
    on_session_start.on_session_start(roots=roots)
    assert exclude.stat().st_mtime_ns == before
    with monkeypatch.context() as patcher:
        calls = []
        original_run = subprocess.run

        def spy_run(*args, **kwargs):
            """Record subprocess commands while preserving their actual results."""
            calls.append(args[0])
            return original_run(*args, **kwargs)

        patcher.setattr(git_exclude.subprocess, "run", spy_run)
        sidecar.bump_use(layer.PROJECT, "learned", root)
        sidecar.bump_view(layer.PROJECT, "learned", root)
        counters.bump_request(layer.PROJECT, root)
        counters.bump_session("hot-counter", root)
        assert calls == []
    for name in ("learned", "unmatched-skill"):
        path = linked_worktree / root.relative_to(main_repo) / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("Untracked worktree instructions.\n")
    worktree_status = status(linked_worktree)
    assert "learned" not in worktree_status
    assert "unmatched-skill/SKILL.md" in worktree_status
    archived = skill_store.archive(layer.PROJECT, "learned", root)
    assert "learned" not in status()
    assert skill_store.restore(layer.PROJECT, archived.name, root) == learned.parent
    (learned.parent / sidecar.FILENAME).unlink()
    on_session_start.on_session_start(roots=roots)
    assert "learned/SKILL.md" in status()  # stale owned rules must not hide a user's replacement
    another = main_repo / "other project" / ".agents"
    skill_store.write_body(layer.PROJECT, "other-skill", "Other instructions.\n", another)
    sidecar.create(layer.PROJECT, "other-skill", 0, another)
    on_session_start.on_session_start(roots=roots)
    assert "other-skill" not in status()  # one root's refresh must retain another root's rules
    owned = main_repo / "symlink-target"
    owned.mkdir()
    (owned / "SKILL.md").write_text("Keep a linked user skill visible.\n")
    sidecar.create(layer.PROJECT, "linked-skill", 0, root)
    (root / "skills" / "linked-skill" / sidecar.FILENAME).replace(owned / sidecar.FILENAME)
    (root / "skills" / "linked-skill").rmdir()
    (root / "skills" / "linked-skill").symlink_to(owned, target_is_directory=True)
    on_session_start.on_session_start(roots=roots)
    assert "linked-skill" in status()
    with monkeypatch.context() as patcher:
        original_land = promoter._land

        def failed_land(*args):
            """Fail after ownership publication to exercise exclusion rollback."""
            original_land(*args)
            raise OSError("after ownership write")

        patcher.setattr(promoter, "_land", failed_land)
        failed = {**intent, "name": "failed-create", "body": intent["body"].replace("learned", "failed-create")}
        assert not promoter.promote(failed, roots=roots)["ok"]
    skill_store.write_body(layer.PROJECT, "failed-create", "User replacement.\n", root)
    assert "failed-create/SKILL.md" in status()
    saved = exclude.read_bytes()
    exclude.unlink()
    outside = tmp_path / "external-exclude"
    outside.write_bytes(saved)
    exclude.symlink_to(outside)
    on_session_start.on_session_start(roots={**roots, layer.PROJECT: another})
    assert outside.read_bytes() == saved and exclude.is_symlink()


def test_project_root_worktree_subdir_maps_to_main_root(linked_worktree, main_repo, monkeypatch):
    sub = linked_worktree / "src"
    sub.mkdir()
    assert _project_root_at(monkeypatch, sub) == main_repo / ".agents"


def test_project_root_worktree_outside_repo_maps_to_main_root(main_repo, tmp_path, monkeypatch):
    wt = tmp_path / "outside-wt"
    _git(main_repo, "worktree", "add", "-q", "-b", "out", str(wt))
    assert _project_root_at(monkeypatch, wt) == main_repo / ".agents"


def test_project_root_git_failure_falls_back_to_cwd(linked_worktree, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git not installed")
    monkeypatch.setattr(layer.subprocess, "run", boom)
    layer._main_worktree_root_resolved.cache_clear()
    assert _project_root_at(monkeypatch, linked_worktree) == linked_worktree / ".agents"
