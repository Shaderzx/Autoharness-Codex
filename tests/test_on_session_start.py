import json

from codex_autoharness import config
from codex_autoharness.hook import on_session_start
from codex_autoharness.lib import layer, sidecar, skill_store


def _roots(base):
    return {"global": base / "g", "project": base / "p"}


def _seed(roots, name, calls, anchor=0, lvl="project"):
    root = roots[lvl]
    skill_store.write_body(lvl, name, f"---\nname: {name}\ndescription: d\n---\nb", root)
    s = sidecar.create(lvl, name, anchor, root)
    s["calls"] = calls
    sidecar.write(lvl, name, s, root)


def _set_requests(roots, lvl, n):
    p = layer.state_dir(lvl, roots[lvl]) / "requests"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n))


def _small_knobs(monkeypatch, cap_project=5):
    monkeypatch.setattr(config, "MATURITY_THRESHOLD", {"global": 10, "project": 10})
    monkeypatch.setattr(config, "CAPACITY", {"global": 5, "project": cap_project})


def test_archives_idle_and_overflow_keeps_strong_and_probation(tmp_path, monkeypatch):
    _small_knobs(monkeypatch, cap_project=1)
    roots = _roots(tmp_path)
    _set_requests(roots, "project", 100)
    _seed(roots, "idle", calls=0)             # mature, rate 0 → bottom of the capacity race
    _seed(roots, "weak", calls=5)             # rate .05, loses capacity race
    _seed(roots, "strong", calls=80)          # rate .8, top of pool → kept
    _seed(roots, "baby", calls=1, anchor=95)  # denom 5 < 10 → probation → survives

    out = on_session_start.on_session_start(roots=roots)
    assert set(out["archived"]["project"]) == {"idle", "weak"}
    assert set(out["archived"]) == {"global", "project"}  # both layers processed
    for gone in ("idle", "weak"):
        assert not skill_store.exists("project", gone, roots["project"])
    assert skill_store.exists("project", "strong", roots["project"])
    assert skill_store.exists("project", "baby", roots["project"])
    assert "strong [project]" in out["context"] and "baby [project]" in out["context"]
    assert "idle [project]" not in out["context"] and "weak [project]" not in out["context"]
    assert on_session_start.on_session_start(roots=roots)["archived"]["project"] == []
    skill_store.restore("project", "weak", roots["project"])  # reversible
    assert skill_store.exists("project", "weak", roots["project"])


def test_native_skill_never_archived(tmp_path, monkeypatch):
    _small_knobs(monkeypatch)
    roots = _roots(tmp_path)
    _set_requests(roots, "project", 100)
    # native: no sidecar, zero usage, mature window — must stay (not a member)
    skill_store.write_body("project", "native",
                           "---\nname: native\ndescription: d\n---\nb", roots["project"])
    out = on_session_start.on_session_start(roots=roots)
    assert "native" not in out["archived"]["project"]
    assert skill_store.exists("project", "native", roots["project"])


def _seed_desc(roots, name, desc, lvl="project", category=None, agent=True):
    root = roots[lvl]
    cat = f"category: {category}\n" if category else ""
    skill_store.write_body(lvl, name, f"---\nname: {name}\ndescription: {desc}\n{cat}---\nb", root)
    if agent:
        sidecar.create(lvl, name, 0, root)


def test_index_excludes_native_and_archived_and_empty_is_none(tmp_path):
    roots = _roots(tmp_path)
    out = on_session_start.on_session_start(roots=roots)
    assert out["context"] is None  # empty library -> zero injection
    _seed_desc(roots, "native", "use when native", agent=False)
    _seed_desc(roots, "mine", "use when mine")
    skill_store.archive("project", "mine", roots["project"])
    out = on_session_start.on_session_start(roots=roots)
    assert out["context"] is None  # native not listed, archived physically out


def test_index_marks_a_truncated_description_as_cut(tmp_path):
    # legacy descriptions predate the budget gate; the reader must be able to tell a line was severed
    # rather than read a fragment as the whole trigger
    roots = _roots(tmp_path)
    long = "Use when auditing " + "x" * config.INDEX_DESC_MAX_CHARS
    _seed_desc(roots, "legacy", long)
    ctx = on_session_start.recall_index(roots)
    line = next(ln for ln in ctx.splitlines() if "legacy" in ln)
    desc = line.split(": ", 1)[1].rsplit(" (file: ", 1)[0]
    assert desc.endswith("...")
    assert len(desc) == config.INDEX_DESC_MAX_CHARS


def test_index_suspended_still_lets_the_summary_through(tmp_path, monkeypatch):
    # suspending the index must not suppress the anti-silence line: they are separate obligations
    roots = _roots(tmp_path)
    _seed_desc(roots, "hidden-skill", "Use when testing suspended recall.")
    state = layer.state_dir("project", roots["project"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "last_run.json").write_text(json.dumps(
        {"run_id": "r1", "landed": 1, "rejected": 0, "absorbed": 0, "families": []}))
    monkeypatch.setattr(config, "INDEX_SUSPENDED", True)
    context = on_session_start.on_session_start(roots=roots)["context"]
    assert "landed 1" in context and "hidden-skill" not in context
    assert on_session_start.on_session_start(roots=roots)["context"] is None


def test_index_points_worktree_session_at_remapped_project_skills(tmp_path):
    # a linked worktree's project layer lives in the main checkout, which the host does not scan:
    # the index must say where the skills are, or they are listed but unloadable
    roots = _roots(tmp_path / "main")  # project dir = tmp/main; the worktree sits beside it
    _seed_desc(roots, "a-skill", "use when a")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = on_session_start.on_session_start({"cwd": str(worktree)}, roots=roots)["context"]
    skills = layer.skills_dir("project", roots["project"]).resolve()
    assert f"{skills}/<name>/SKILL.md" in ctx
