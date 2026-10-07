"""Claude imports preserve user ownership, support files and discovery boundaries."""
import errno
import json
import os
import stat
from types import SimpleNamespace

import pytest

from codex_autoharness import cli, integration
from codex_autoharness.hook import on_session_start
from codex_autoharness.lib import sidecar, skill_import


def _skill(base, name="existing-claude"):
    """Create a Claude skill with executable support, binary assets and ownership metadata."""
    path = base / ".claude" / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when inspecting existing skills.\n---\nRead scripts/check.py.\n",
        encoding="utf-8",
    )
    script = path / "scripts" / "check.py"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env python3\nprint('ready')\n", encoding="utf-8")
    script.chmod(0o755)
    (path / "assets").mkdir()
    (path / "assets" / "sample.bin").write_bytes(b"\x00\xff\x10")
    (path / ".sidecar.json").write_text(json.dumps({"created_by": sidecar.OWNER}))
    (path / ".ledger.jsonl").write_text('{"old": true}\n')
    return path


def _roots(tmp_path):
    """Select distinct temporary homes for the global and project skill layers."""
    return {"global": tmp_path / "home" / ".agents", "project": tmp_path / "repo" / ".agents"}


def test_import_preserves_files_and_modes_without_ownership_or_overwrites(tmp_path):
    """Imports preserve source bytes and executable bits without ownership or later overwrite."""
    roots = _roots(tmp_path)
    sources = {level: _skill(root.parent) for level, root in roots.items()}
    original = {level: {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
                for level, source in sources.items()}

    result = skill_import.import_skills(roots)
    for level, root in roots.items():
        assert result[level] == {"imported": ["existing-claude"], "skipped": {}}
        copied = root / "skills" / "existing-claude"
        assert (copied / "SKILL.md").read_bytes() == (sources[level] / "SKILL.md").read_bytes()
        assert (copied / "assets" / "sample.bin").read_bytes() == b"\x00\xff\x10"
        assert stat.S_IMODE((copied / "scripts" / "check.py").stat().st_mode) == 0o755
        assert not (copied / ".sidecar.json").exists()
        assert not (copied / ".ledger.jsonl").exists()
        assert not sidecar.is_agent_created(level, "existing-claude", root)
        for rel, contents in original[level].items():
            assert (sources[level] / rel).read_bytes() == contents
        (copied / "SKILL.md").write_text("User's Codex revision.")
        (sources[level] / "SKILL.md").write_text("New Claude revision.")

    again = skill_import.import_skills(roots)
    for level, root in roots.items():
        assert again[level] == {"imported": [], "skipped": {"existing-claude": "destination exists"}}
        assert (root / "skills" / "existing-claude" / "SKILL.md").read_text() == "User's Codex revision."


@pytest.mark.parametrize("collision", ["directory", "file", "symlink", "dangling-link"])
def test_existing_destination_is_never_replaced(tmp_path, collision):
    """Existing files, directories and symlinks remain untouched during import."""
    source = _skill(tmp_path)
    root = tmp_path / ".agents"
    dest = root / "skills" / source.name
    dest.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if collision == "directory":
        dest.mkdir()
    elif collision == "file":
        dest.write_text("user file")
    else:
        dest.symlink_to(outside if collision == "symlink" else tmp_path / "missing", target_is_directory=True)

    result = skill_import.import_layer("project", root)

    assert result == {"imported": [], "skipped": {source.name: "destination exists"}}
    assert not list(outside.iterdir())
    if collision == "directory":
        assert not list(dest.iterdir())
    elif collision == "file":
        assert dest.read_text() == "user file"
    else:
        assert dest.is_symlink()


@pytest.mark.parametrize("unsafe", ["skill-link", "body-link", "file-link", "directory-link", "fifo", "name"])
def test_unsafe_sources_are_skipped_and_partial_copy_removed(tmp_path, unsafe):
    """Unsafe names, links and special files cannot publish a partial skill."""
    source = _skill(tmp_path, "bad..name" if unsafe == "name" else "unsafe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("stay outside")
    if unsafe == "skill-link":
        source.rename(source.with_name("original"))
        source.symlink_to(source.with_name("original"), target_is_directory=True)
    elif unsafe == "body-link":
        (source / "SKILL.md").unlink()
        (source / "SKILL.md").symlink_to(outside / "secret")
    elif unsafe == "file-link":
        (source / "scripts" / "linked").symlink_to(outside / "secret")
    elif unsafe == "directory-link":
        (source / "linked").symlink_to(outside, target_is_directory=True)
    elif unsafe == "fifo":
        os.mkfifo(source / "pipe")

    root = tmp_path / ".agents"
    result = skill_import.import_layer("project", root)

    assert source.name in result["skipped"]
    assert not (root / "skills" / source.name).exists()
    assert (outside / "secret").read_text() == "stay outside"


@pytest.mark.parametrize("redirect", [".claude", ".claude/skills", ".agents", ".agents/skills"])
def test_symlinked_discovery_root_is_refused(tmp_path, redirect):
    """Redirected discovery roots cannot write into an unrelated outside directory."""
    source = _skill(tmp_path)
    path = tmp_path / redirect
    if path.exists():
        path.rename(path.with_name(path.name + "-original"))
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)

    if redirect == ".agents":
        with pytest.raises(ValueError):
            skill_import.import_layer("project", tmp_path / ".agents")
    else:
        result = skill_import.import_layer("project", tmp_path / ".agents")
        assert result["imported"] == []
        assert result["skipped"]
    assert not list(outside.iterdir())
    assert source.name == "existing-claude"


def test_missing_claude_directory_does_not_create_state(tmp_path):
    """An absent Claude library leaves the selected home untouched."""
    root = tmp_path / "absent" / ".agents"
    assert skill_import.import_layer("global", root) == {"imported": [], "skipped": {}}
    assert not root.parent.exists()


@pytest.mark.parametrize("scope", ["global", "project"])
def test_install_automatically_imports_only_its_scope_and_uninstall_retains_it(tmp_path, scope):
    """Installation imports its selected scope and uninstall retains those user-owned copies."""
    home, project = tmp_path / "home", tmp_path / "repo"
    _skill(home)
    _skill(project)
    kwargs = {"home": home, **({"project": project} if scope == "project" else {})}

    result = integration.install(**kwargs)
    target, other = (project, home) if scope == "project" else (home, project)
    assert result["skill_import"]["imported"] == ["existing-claude"]
    assert not (other / ".agents").exists()
    integration.uninstall(**kwargs)
    assert (target / ".agents" / "skills" / "existing-claude" / "SKILL.md").is_file()


def test_session_start_imports_both_scopes_and_offers_new_paths_without_management(tmp_path):
    """Startup exposes newly imported paths while excluding them from managed lifecycle recall."""
    roots = _roots(tmp_path)
    for root in roots.values():
        _skill(root.parent)

    result = on_session_start.on_session_start(roots=roots)

    assert result["archived"] == {"global": [], "project": []}
    assert "user-owned Codex skills" in result["context"]
    for level, root in roots.items():
        assert result["skill_import"][level]["imported"] == ["existing-claude"]
        assert str(root / "skills" / "existing-claude" / "SKILL.md") in result["context"]
    assert on_session_start.recall_index(roots) is None
    assert on_session_start.on_session_start(roots=roots)["context"] is None


def test_import_context_is_bounded_and_points_to_remaining_skills(tmp_path):
    """Startup limits imported descriptors and points readers to the remaining skill files."""
    roots = _roots(tmp_path)
    for number in range(25):
        _skill(roots["project"].parent, f"skill-{number}")
    context = on_session_start.on_session_start(roots=roots)["context"]
    assert len([line for line in context.splitlines() if line.startswith("- ")]) == 20
    assert "5 more imported skills" in context
    assert str(roots["project"] / "skills") in context


def test_cli_import_reports_both_scopes_and_deduplicates_home_project(tmp_path, capsys):
    """The CLI reports shared home/project storage as one imported global layer."""
    _skill(tmp_path)
    assert cli.main(["--home", str(tmp_path), "--project", str(tmp_path), "import-skills"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"]
    assert result["layers"] == {"global": {"imported": ["existing-claude"], "skipped": {}}}


def test_cli_reports_unsafe_import_as_unsuccessful(tmp_path, capsys):
    """An unsafe source produces a nonzero CLI result with a specific skip reason."""
    source = _skill(tmp_path)
    (source / "linked").symlink_to(source / "SKILL.md")
    assert cli.main(["--home", str(tmp_path), "--project", str(tmp_path), "import-skills"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"]
    assert "symlink" in result["layers"]["global"]["skipped"]["existing-claude"]


def test_interrupted_staging_is_outside_discovery_and_cleaned_before_retry(tmp_path):
    """Retry removes interrupted staging before publishing a complete discoverable skill."""
    _skill(tmp_path)
    root = tmp_path / ".agents"
    stale = root / "codex-autoharness" / "imports" / ".claude-import-killed" / "existing-claude"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("interrupted copy")
    assert not list((root / "skills").glob("*/SKILL.md"))

    result = skill_import.import_layer("project", root)

    assert result["imported"] == ["existing-claude"]
    assert not stale.parent.exists()
    assert not list((root / "codex-autoharness" / "imports").iterdir())


def test_destination_created_during_copy_is_preserved_even_when_empty(tmp_path, monkeypatch):
    """Atomic publication preserves a destination created after the initial collision check."""
    source = _skill(tmp_path)
    root = tmp_path / ".agents"
    original = skill_import._publish

    def race(src, name, dst):
        """Create an empty user destination immediately before native publication."""
        (root / "skills" / name).mkdir()
        return original(src, name, dst)

    monkeypatch.setattr(skill_import, "_publish", race)
    result = skill_import.import_layer("project", root)
    assert result == {"imported": [], "skipped": {source.name: "destination exists"}}
    assert not list((root / "skills" / source.name).iterdir())
    assert not list((root / "codex-autoharness" / "imports").iterdir())


def test_copy_failure_can_be_retried_without_a_partial_destination(tmp_path, monkeypatch):
    """A failed staged copy leaves no live destination and can succeed on retry."""
    _skill(tmp_path)
    root = tmp_path / ".agents"
    original = skill_import._copy_tree

    def fail(src, dst, **kwargs):
        """Write a partial staged file and inject a copy failure."""
        fd = os.open("partial", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst)
        os.close(fd)
        raise OSError("simulated copy failure")

    monkeypatch.setattr(skill_import, "_copy_tree", fail)
    assert skill_import.import_layer("project", root)["imported"] == []
    assert not (root / "skills" / "existing-claude").exists()
    monkeypatch.setattr(skill_import, "_copy_tree", original)
    assert skill_import.import_layer("project", root)["imported"] == ["existing-claude"]


def test_startup_timeout_keeps_retryable_sources_and_reports_manual_command(tmp_path, monkeypatch):
    """An expired startup budget preserves sources and points users to the manual importer."""
    roots = _roots(tmp_path)
    _skill(roots["global"].parent)
    _skill(roots["project"].parent)
    ticks = iter([0, 6, 7])
    monkeypatch.setattr(skill_import.time, "monotonic", lambda: next(ticks))
    result = on_session_start.on_session_start(roots=roots)
    assert "import-skills" in result["context"]
    for level, root in roots.items():
        assert result["skill_import"][level]["imported"] == []
        assert "time budget" in result["skill_import"][level]["skipped"]["existing-claude"]
        assert not (root / "skills" / "existing-claude").exists()


def _native_error(monkeypatch, error):
    """Replace native rename with a recording callable that returns the requested errno."""
    class Rename:
        """Record publication attempts while simulating a native rename failure."""
        def __init__(self):
            """Initialize the native-call record."""
            self.calls = []

        def __call__(self, *args):
            """Record arguments and expose the configured errno to the importer."""
            self.calls.append(args)
            skill_import.ctypes.set_errno(error)
            return -1

    rename = Rename()
    native = SimpleNamespace(renameatx_np=rename, renameat2=rename)
    monkeypatch.setattr(skill_import.ctypes, "CDLL", lambda *args, **kwargs: native)
    return rename


@pytest.mark.parametrize("native_errno", [errno.EINVAL, errno.ENOSYS, errno.ENOTSUP])
def test_unsupported_native_rename_stops_layer_after_first_copy(tmp_path, monkeypatch, native_errno):
    """Unsupported publication stops the layer after its first staged attempt."""
    for name in ("first", "second", "third"):
        _skill(tmp_path, name)
    root = tmp_path / ".agents"
    rename = _native_error(monkeypatch, native_errno)

    result = skill_import.import_layer("project", root)

    assert result["imported"] == []
    assert len(rename.calls) == 1
    assert "atomic no-replace import is unsupported on this filesystem" in result["skipped"]["first"]
    assert "remaining imports stopped" in result["skipped"]["."]
    assert not list((root / "skills").iterdir())
    assert not list((root / "codex-autoharness" / "imports").iterdir())


@pytest.mark.parametrize("native_errno", [errno.EEXIST, errno.EACCES])
def test_other_native_errors_keep_errno_and_do_not_stop_layer(tmp_path, monkeypatch, native_errno):
    """Collision and permission failures retain their errno and allow later skill attempts."""
    for name in ("first", "second"):
        _skill(tmp_path, name)
    rename = _native_error(monkeypatch, native_errno)
    with pytest.raises(OSError) as error:
        skill_import._publish(1, "name", 2)
    assert error.value.errno == native_errno

    result = skill_import.import_layer("project", tmp_path / ".agents")

    assert len(rename.calls) == 3
    assert set(result["skipped"]) == {"first", "second"}
    if native_errno == errno.EEXIST:
        assert set(result["skipped"].values()) == {"destination exists"}


def test_missing_native_symbol_is_reported_before_copying_any_skill(tmp_path, monkeypatch):
    """A missing exclusive-rename symbol is detected before copying or creating state."""
    _skill(tmp_path)
    monkeypatch.setattr(skill_import.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())

    def unexpected_copy(*args, **kwargs):
        """Fail the regression if an unavailable native API still permits copying."""
        pytest.fail("unsupported native API should be detected before copying")

    monkeypatch.setattr(skill_import, "_copy_tree", unexpected_copy)
    root = tmp_path / ".agents"
    result = skill_import.import_layer("project", root)
    assert result["imported"] == []
    assert "atomic no-replace import is unsupported on this filesystem" in result["skipped"]["."]
    assert not root.exists()
