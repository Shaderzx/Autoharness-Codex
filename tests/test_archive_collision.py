"""Regression: archive/restore must not destroy existing data on name collision."""
from unittest.mock import patch

import pytest

from codex_autoharness.lib import layer, sidecar, skill_store


def _make_tree(root, lyr, name, marker="live"):
    """Create a fake skill dir with a marker file."""
    skills = layer.symbol_dir(lyr, name, root)
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use when testing.\n---\nmarker={marker}")
    sidecar.create(lyr, name, 0, root)
    return skills


def test_archive_collision_preserves_old(tmp_path):
    """Archiving a skill when an archive of the same name exists must not destroy the old archive."""
    root = str(tmp_path)
    # Simulate: skill exists in archive AND in live
    _make_tree(root, "global", "foo", marker="archived")
    archive_dest = layer.archive_dir("global", root) / "foo"
    archive_dest.mkdir(parents=True, exist_ok=True)
    (archive_dest / "SKILL.md").write_text("# foo\nmarker=old-archive")

    result = skill_store.archive("global", "foo", root)

    # The old archive must still exist
    assert (archive_dest / "SKILL.md").read_text() == "# foo\nmarker=old-archive"
    # The new archive landed at a timestamped path
    assert result is not None
    assert result != archive_dest
    assert "marker=archived" in (result / "SKILL.md").read_text()


def test_restore_collision_preserves_live(tmp_path):
    """Restoring a skill when a live copy exists must not destroy the live copy."""
    root = str(tmp_path)
    _make_tree(root, "global", "foo", marker="from-archive")
    archive_src = skill_store.archive("global", "foo", root)
    _make_tree(root, "global", "foo", marker="live-current")

    with pytest.raises(ValueError, match="already exists"):
        skill_store.restore("global", "foo", root)

    # The original live copy must still exist
    live = layer.symbol_dir("global", "foo", root)
    assert "marker=live-current" in (live / "SKILL.md").read_text()
    assert "marker=from-archive" in (archive_src / "SKILL.md").read_text()


def test_archive_repeated_timestamp_collision_gets_unique_suffix(tmp_path):
    root = str(tmp_path)
    archive_dest = layer.archive_dir("global", root) / "foo"
    archive_dest.mkdir(parents=True)
    (archive_dest / "SKILL.md").write_text("# foo\nmarker=old-archive")

    with patch("codex_autoharness.lib.skill_store.time.strftime", return_value="20260929T120000"):
        _make_tree(root, "global", "foo", marker="first")
        first = skill_store.archive("global", "foo", root)
        _make_tree(root, "global", "foo", marker="second")
        second = skill_store.archive("global", "foo", root)

    assert first.name == "foo.20260929T120000"
    assert second.name == "foo.20260929T120000.2"
    assert (archive_dest / "SKILL.md").read_text() == "# foo\nmarker=old-archive"
    assert "marker=first" in (first / "SKILL.md").read_text()
    assert "marker=second" in (second / "SKILL.md").read_text()


def test_timestamped_archive_restores_original_skill_identity(tmp_path):
    _make_tree(tmp_path, "global", "foo", marker="old")
    skill_store.archive("global", "foo", tmp_path)
    _make_tree(tmp_path, "global", "foo", marker="new")
    newer = skill_store.archive("global", "foo", tmp_path)
    assert newer.name != "foo"
    restored = skill_store.restore("global", newer.name, tmp_path)
    assert restored.name == "foo"
    assert "marker=new" in skill_store.read_body("global", "foo", tmp_path)
    assert sidecar.is_agent_created("global", "foo", tmp_path)
