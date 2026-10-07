"""Skill content rules not already exercised by staging and promotion tests."""
from codex_autoharness import config
from codex_autoharness.lib import validate

BODY = "---\nname: foo\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"
INTENT = {"action": "create", "name": "foo", "level": "project", "reason": "r", "evidence": "e"}


def test_new_skill_budgets_allow_boundary_and_legacy_patches():
    frontmatter = BODY.rsplit("Use ISO dates.", 1)[0]
    boundary = frontmatter + "Rule.\n\n" * config.SKILL_BODY_MAX_LINES
    assert validate.validate(INTENT, boundary)["ok"]
    too_long = boundary + "One extra rule.\n"
    assert "altitude" in dict(validate.validate(INTENT, too_long)["findings"])
    legacy = too_long.replace("Use when formatting dates.", "Legacy topic " * 15)
    result = validate.validate(INTENT, legacy)
    assert {"altitude", "description", "trigger"} <= dict(result["findings"]).keys()
    patch = {**INTENT, "action": "patch"}
    assert validate.validate(patch, legacy, target_is_agent_created=True)["ok"]


def test_content_completeness_accepts_callouts_but_rejects_placeholders():
    assert validate.validate(INTENT, BODY + "See <NOTE> and docs/TODO.md.\n")["ok"]
    for token in ("TODO:", "FIXME:", "<PLACEHOLDER>"):
        result = validate.validate(INTENT, BODY + token)
        assert "completeness" in dict(result["findings"]), token
    missing = BODY.replace("description: Use when formatting dates.\n", "")
    assert "structure" in dict(validate.validate(INTENT, missing)["findings"])


def test_trigger_quotes_and_category_validation():
    quoted = BODY.replace("Use when formatting dates.", "Fires on 'format dates'.")
    assert validate.validate(INTENT, quoted)["ok"]
    for category in ("a/b", "..", "a b", ""):
        body = BODY.replace("name: foo\n", f"name: foo\ncategory: {category}\n")
        assert "category" in dict(validate.validate(INTENT, body)["findings"]), category


def test_python_references_validate_local_files_without_reading_outside(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    (base / "helper.py").write_text("def broken(:\n")
    (tmp_path / "secret.py").write_text("def broken(:\n")
    local = validate.validate(INTENT, BODY + "See helper.py\n", base_dir=base)
    assert any("helper.py has syntax error" in message for _, message in local["findings"])
    outside = validate.validate(INTENT, BODY + "See ../secret.py\n", base_dir=base)
    messages = [message for _, message in outside["findings"]]
    assert any("escapes the skill directory" in message for message in messages)
    assert not any("secret.py has syntax error" in message for message in messages)
    escaped_support = validate.validate(INTENT, BODY + "See references/../../outside/x.md\n", base_dir=base)
    assert not any("outside/x.md" in message for _, message in escaped_support["findings"])


def test_support_reference_must_be_carried_or_already_live(tmp_path):
    body = BODY + "Run scripts/check.py\n"
    assert not validate.validate(INTENT, body, base_dir=tmp_path)["ok"]
    files = {"scripts/check.py": "print('ISO')\n"}
    assert validate.validate({**INTENT, "files": files}, body, base_dir=tmp_path)["ok"]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check.py").write_text(files["scripts/check.py"])
    assert validate.validate(INTENT, body, base_dir=tmp_path)["ok"]


def test_global_support_files_cannot_embed_project_paths():
    proposal = {**INTENT, "level": "global", "files": {"references/notes.md": "Run /home/example/project/run.py\n"}}
    result = validate.validate(proposal, BODY + "See references/notes.md\n")
    assert "global_repo_agnostic" in dict(result["findings"])
