import subprocess

import pytest

from codex_autoharness.lib import layer


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
