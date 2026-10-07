"""Metrics reconcile with persisted usage, provenance and run outcomes."""
import json

from codex_autoharness.lib import counters, ledger, metrics, sidecar, skill_store


def seed(root, name, **counts):
    skill_store.write_body("project", name, f"---\nname: {name}\ndescription: Use when testing.\n---\nRule.\n", root)
    metadata = sidecar.create("project", name, 0, root)
    sidecar.write("project", name, {**metadata, **counts}, root)


def test_usage_funnel_and_categories_are_read_only(tmp_path):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    root = roots["project"]
    empty = metrics.collect(roots)["project"]
    assert empty["recall_rate"] == 0 and empty["funnel"]["proposed"] == 0
    for _ in range(10):
        counters.bump_request("project", root)
    seed(root, "improved", use=2, view=1, patch=1, reused_gen=1)
    seed(root, "stale", use=1, patch=2, reused_gen=1)
    seed(root, "unused")
    body = skill_store.read_body("project", "stale", root).replace("name: stale\n", "name: stale\ncategory: dates\n")
    skill_store.write_body("project", "stale", body, root)
    runs = root / "codex-autoharness/runs"
    runs.mkdir()
    (runs / "one.json").write_text(json.dumps({"verdicts": [
        {"ok": True, "findings": []}, {"ok": False, "findings": ["structure"]}]}))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = metrics.collect(roots)["project"]
    assert result["use_total"] == 3 and result["view_total"] == 1
    assert result["recall_rate"] == 3 / 10 and result["used_symbol_share"] == 2 / 3
    assert result["reuse_after_patch"] == 1 / 2
    assert result["funnel"] == {"proposed": 2, "landed": 1, "rejected": 1}
    assert result["reject_families"] == {"structure": 1}
    categories = result["by_category"]
    assert categories["general"]["live_symbols"] == 2
    assert categories["dates"]["recall_rate"] == 1 / 10
    assert sum(c["use_total"] for c in categories.values()) == result["use_total"]
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_archive_causes_partition_the_archived_skills(tmp_path):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    root = roots["project"]
    for name in ("merged", "pruned", "evicted"):
        seed(root, name)
        if name != "evicted":
            ledger.append("project", name, {"action": "delete", "absorbed_into": "umbrella" if name == "merged" else ""}, root)
        skill_store.archive("project", name, root)
    result = metrics.collect(roots)["project"]
    assert result["deaths"] == {"absorbed": 1, "pruned": 1, "lifecycle": 1}
    assert sum(result["deaths"].values()) == result["archived_total"] == 3
