"""Portable plugin launcher; detached workers inherit this checkout's package path."""
import os
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    print("codex-autoharness requires Python 3.11 or newer", file=sys.stderr)
    raise SystemExit(1)

source = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, source)
os.environ["PYTHONPATH"] = source + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")

from codex_autoharness.cli import main  # noqa: E402

raise SystemExit(main())
