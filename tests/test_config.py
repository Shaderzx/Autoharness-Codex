"""Documented environment overrides and invalid-value fallback."""
import importlib

from codex_autoharness import config


def test_environment_overrides_and_invalid_integer_fallback(monkeypatch):
    values = {"ENABLED": "0", "REFLECT_EVERY_N": "2", "CONSOLIDATE_EVERY_N": "0",
              "MATURITY_PROJECT": "3", "CAPACITY_PROJECT": "1", "GRADUATION_SUSPENDED": "1",
              "INDEX_SUSPENDED": "1", "SNAPSHOT_KEEP": "11", "NOTIFY": " Desktop ",
              "NOTIFY_TIMEOUT_S": "0"}
    try:
        with monkeypatch.context() as env:
            for key, value in values.items():
                env.setenv("CODEX_AUTOHARNESS_" + key, value)
            importlib.reload(config)
            assert not config.ENABLED and config.CONSOLIDATE_EVERY_N == 0
            assert config.REFLECT_EVERY_N == 2 and config.MATURITY_THRESHOLD["project"] == 3
            assert config.CAPACITY["project"] == 1
            assert config.GRADUATION_REVIEW_SUSPENDED and config.INDEX_SUSPENDED
            assert config.SNAPSHOT_KEEP == 11
            assert config.NOTIFY == "desktop" and config.NOTIFY_TIMEOUT_S == 1
            env.setenv("CODEX_AUTOHARNESS_SNAPSHOT_KEEP", "five")
            importlib.reload(config)
            assert config.SNAPSHOT_KEEP == 5
    finally:
        importlib.reload(config)
