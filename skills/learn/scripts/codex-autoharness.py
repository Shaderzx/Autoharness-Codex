"""Locate the plugin launcher relative to this installed skill."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[3] / "bin" / "codex-autoharness.py"), run_name="__main__")
