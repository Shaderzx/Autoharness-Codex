"""Storage behavior at interruption and concurrent mutation boundaries."""
import json

import pytest

from codex_autoharness.hook import promoter
from codex_autoharness.lib import (
    intent_queue,
    layer,
    ledger,
    redact,
    sidecar,
    skill_store,
)

BODY = "---\nname: dates\ndescription: Use when formatting dates.\ncategory: testing\n---\nUse ISO dates.\n"


def proposal(**changes):
    return {"action": "create", "name": "dates", "level": "project", "body": BODY,
            "reason": "Date output was corrected", "evidence": "Use ISO dates.", **changes}


def tree(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def test_completed_promotion_is_not_applied_twice_after_drain_interruption(tmp_path, monkeypatch):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    intent_queue.append("recover", proposal(), roots["project"])
    account = promoter._account

    def fail_account(*args):
        raise OSError("simulated interruption before queue clearing")

    monkeypatch.setattr(promoter, "_account", fail_account)
    with pytest.raises(OSError):
        promoter.drain("recover", roots=roots)
    assert len(ledger.read("project", "dates", roots["project"])) == 1
    monkeypatch.setattr(promoter, "_account", account)
    result = promoter.drain("recover", roots=roots)
    assert result[0]["ok"] and result[0]["replayed"]
    assert len(ledger.read("project", "dates", roots["project"])) == 1
    assert intent_queue.read("recover", roots["project"]) == []


def test_partial_write_exception_restores_complete_previous_skill(tmp_path, monkeypatch):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote(proposal(), roots=roots)["ok"]
    directory = layer.symbol_dir("project", "dates", roots["project"])
    before = tree(directory)

    def fail_land(action, intent, body, level, name, root):
        skill_store.write_body(level, name, body, root)
        (directory / "references" / "partial.md").write_text("incomplete mutation")
        raise OSError("disk failure")

    monkeypatch.setattr(promoter, "_land", fail_land)
    update = proposal(action="update", body=BODY.replace("ISO", "UTC"))
    update.pop("level")
    assert not promoter.promote(update, roots=roots)["ok"]
    assert tree(directory) == before


def test_other_agents_ownership_marker_is_not_ours(tmp_path):
    directory = layer.symbol_dir("project", "other", tmp_path)
    directory.mkdir(parents=True)
    (directory / sidecar.FILENAME).write_text(json.dumps({"created_by": "agent"}))
    assert not sidecar.is_agent_created("project", "other", tmp_path)
    with pytest.raises(ValueError, match="unmanaged"):
        skill_store.archive("project", "other", tmp_path)


def test_sweep_preserves_all_unmanaged_temporary_files(tmp_path):
    directory = layer.symbol_dir("project", "other", tmp_path)
    directory.mkdir(parents=True)
    for name in ("handwritten.tmp", ".codex-autoharness-SKILL.md.abcd.tmp"):
        (directory / name).write_text("keep")
    assert skill_store.sweep_orphans("project", tmp_path) == []
    assert len(list(directory.iterdir())) == 2


def test_private_key_redaction_covers_whole_block_and_json_escaped_lines():
    pem = "-----BEGIN PRIVATE KEY-----\nFAKEKEYMATERIAL0123456789\n-----END PRIVATE KEY-----"
    for value in (pem, json.dumps({"output": pem})):
        cleaned = redact.redact(value)
        assert "FAKEKEYMATERIAL" not in cleaned
        assert "REDACTED" in cleaned
        assert redact.redact(cleaned) == cleaned


@pytest.mark.parametrize("retire_first", [False, True])
def test_failed_umbrella_update_cannot_archive_absorbed_skill(tmp_path, retire_first):
    """Preserve a sibling when its absorbing update fails in either proposal order."""
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote(proposal(), roots=roots)["ok"]
    child = proposal(name="date-case", body=BODY.replace("name: dates", "name: date-case"))
    assert promoter.promote(child, roots=roots)["ok"]
    intents = [{"action": "patch", "name": "dates", "old_string": "absent text",
                "new_string": "extra rule", "reason": "fold", "evidence": "overlap"},
               {"action": "delete", "name": "date-case", "absorbed_into": "dates",
                "reason": "fold", "evidence": "overlap"}]
    intent_queue.append_many("fold", intents[::-1] if retire_first else intents, roots["project"])
    results = promoter.drain("fold", roots=roots)
    assert not any(row["ok"] for row in results)
    assert skill_store.exists("project", "date-case", roots["project"])


@pytest.mark.parametrize("frontmatter", [
    "name: dates\ndescription: \"Use when dates fail.'",
    "name: dates\nname: other\ndescription: Use when dates fail.",
    "name: dates\ndescription: Use when dates fail: ISO format.",
    "name: dates\ndescription: true",
    "name: dates\ndescription: 12345",
    "name: dates\ndescription: &shared Use when dates fail.",
    "name: dates\ndescription: >\n  Use when dates fail.",
    "name: dates\ndescription: [Use when dates fail.]",
])
def test_invalid_native_frontmatter_cannot_be_published(tmp_path, frontmatter):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    result = promoter.promote(proposal(body=f"---\n{frontmatter}\n---\nUse ISO dates.\n"), roots=roots)
    assert not result["ok"]
    assert not layer.symbol_dir("project", "dates", roots["project"]).exists()


def test_quoted_description_preserves_routing_text(tmp_path):
    from codex_autoharness.lib import validate
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    description = 'Use when dates fail: "ISO" output.'
    body = f"---\nname: dates\ndescription: {json.dumps(description)}\n---\nUse ISO dates.\n"
    assert promoter.promote(proposal(body=body), roots=roots)["ok"]
    assert validate._frontmatter(skill_store.read_body("project", "dates", roots["project"]))["description"] == description


def test_nested_json_credentials_are_redacted_before_storage():
    credential = "fixture-password-value-0123456789"
    raw = json.dumps({"tool_output": json.dumps({"password": credential})})
    assert credential not in redact.redact(raw)
