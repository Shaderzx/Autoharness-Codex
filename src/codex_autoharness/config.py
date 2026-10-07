"""Runtime settings for Codex AutoHarness. Defaults require calibration in real use.

All operator overrides use the CODEX_AUTOHARNESS_ prefix. Child-session state is
separate from host environment flags so ordinary Codex hooks remain active.
"""
import os
from pathlib import Path

from codex_autoharness.lib import layer


def _int_env(name, default, minimum=None):
    """Read an integer override, enforcing its floor or using the default."""
    try:
        floor = (0 if default == 0 else 1) if minimum is None else minimum
        return max(floor, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


# trigger cadence, counted in the activity quantum (main-session tool calls, direction H) — not turns.
# Recalibrated with the numerator: the old turn-based 10 lands near 30-50 tool calls, and the standing
# ruling is to err sparse (precipitation is maintenance, and SessionEnd flushes the tail).
REFLECT_EVERY_N = _int_env("CODEX_AUTOHARNESS_REFLECT_EVERY_N", 50)  # the window itself is watermark-delimited (capture)
CONSOLIDATE_EVERY_N = _int_env("CODEX_AUTOHARNESS_CONSOLIDATE_EVERY_N", 250, minimum=0)

# raw-capture byte caps (ponytail: placeholders, calibrate in experiments/): per transcript record,
# and per handoff window (tail kept) — bound tool dumps / base64 away from the child context.
CAPTURE_MAX_RECORD_BYTES = 4_000
CAPTURE_MAX_WINDOW_BYTES = 200_000
DIGEST_EXCHANGES = _int_env("CODEX_AUTOHARNESS_DIGEST_EXCHANGES", 20)  # prior-context digest: exchanges before the window
DIGEST_MAX_RECORD_CHARS = 200
DIGEST_MAX_BYTES = 20_000

STAGE_MAX_BODY_BYTES = 100_000  # ponytail: placeholder, SKILL.md body cap; for instant feedback on stage_skill args
# altitude gate: SKILL.md body must read as a rule, not a transcript. A deterministic proxy for the
# reflector (the only LLM in the loop) actually distilling — non-blank body lines (ex-frontmatter)
# over this reject create/update; detail belongs in references/. ponytail: crude proxy, calibrate in experiments/.
SKILL_BODY_MAX_LINES = _int_env("CODEX_AUTOHARNESS_SKILL_BODY_MAX_LINES", 25)
# description is the trigger: the host preloads name+description and matches recall on it, so a skill
# lives or dies here. The independent hard cap is 1024 characters; the recall index has a tighter budget.
SKILL_DESC_MAX_CHARS = _int_env("CODEX_AUTOHARNESS_SKILL_DESC_MAX_CHARS", 1024)
# self-injected recall index (SessionStart additionalContext): per-line description truncation,
# mirroring Hermes's tier-0 index discipline — the index is a scan surface, not the full trigger text.
INDEX_DESC_MAX_CHARS = _int_env("CODEX_AUTOHARNESS_INDEX_DESC_MAX_CHARS", 60)
# self-injection off switch: the index stops being emitted, everything else (lifecycle pass,
# use/view counters, the last-run summary) is untouched. Needed because the only other way to run
# without the index is to run without the plugin — which also removes the counters that measure the
# result. Also a legitimate operator knob for anyone unwilling to spend the context every session.
INDEX_SUSPENDED = bool(_int_env("CODEX_AUTOHARNESS_INDEX_SUSPENDED", 0))

# folder-skill subfile caps (ponytail: placeholders like STAGE_MAX_BODY_BYTES, calibrate in experiments/)
STAGE_MAX_FILES = 8
STAGE_MAX_FILE_BYTES = 64_000
STAGE_MAX_FILES_TOTAL_BYTES = 256_000

# maturity threshold (denominator gate to graduate out of probation) / capacity cap, set per layer, independently tunable. global has a larger blast radius -> more conservative (smaller capacity).
MATURITY_THRESHOLD = {layer.GLOBAL: _int_env("CODEX_AUTOHARNESS_MATURITY_GLOBAL", 300),
                      layer.PROJECT: _int_env("CODEX_AUTOHARNESS_MATURITY_PROJECT", 100)}
CAPACITY = {layer.GLOBAL: _int_env("CODEX_AUTOHARNESS_CAPACITY_GLOBAL", 20),
            layer.PROJECT: _int_env("CODEX_AUTOHARNESS_CAPACITY_PROJECT", 50)}
# graduation-review suspend gate (direction C): while the recall surface is known-broken, archiving
# for zero use buries surfacing's failure — flip on to park the review, capacity contention unaffected.
GRADUATION_REVIEW_SUSPENDED = bool(_int_env("CODEX_AUTOHARNESS_GRADUATION_SUSPENDED", 0))
SNAPSHOT_KEEP = _int_env("CODEX_AUTOHARNESS_SNAPSHOT_KEEP", 5)  # curator pre-run library snapshots per layer (mirrors Hermes)

# run-account notification (lib/notify), opt-in: the SessionStart summary is a session late and
# carries counts, not names. "desktop" = native notification; NOTIFY_CMD = argv fed the run record
# on stdin. Fail-open, fired after the queue is cleared; the timeout (whole seconds, per channel,
# floor 1) bounds how long a notifier can hold a drain — the interactive one runs inside Stop.
NOTIFY = os.environ.get("CODEX_AUTOHARNESS_NOTIFY", "").strip().lower()
NOTIFY_CMD = os.environ.get("CODEX_AUTOHARNESS_NOTIFY_CMD", "")
NOTIFY_TIMEOUT_S = max(1, _int_env("CODEX_AUTOHARNESS_NOTIFY_TIMEOUT_S", 5))

_LIB = Path(__file__).parent / "lib"
REDACTION_RULES = _LIB / "redaction_rules.toml"  # secret/PII rule set, single source for CAP egress + LED
FORMAT_SPEC = _LIB / "format_spec.md"            # #416 single source for authoring + lint

CHILD_SESSION_ENV = "CODEX_AUTOHARNESS_CHILD_SESSION"

# Session reuse is opt-in and only ever reads our isolated learner rollouts.
REFLECTOR_CARRIER = os.environ.get("CODEX_AUTOHARNESS_REFLECTOR_CARRIER", "bundle").strip().lower()

REFLECTOR_AGENT = "codex-autoharness-reflector"
CURATOR_AGENT = "codex-autoharness-curator"
CODEX_BIN = os.environ.get("CODEX_AUTOHARNESS_CODEX_BIN", "codex")                       # the child-session executable for spawn; PATH resolution, overridable in tests
RUN_ID_ENV = "CODEX_AUTOHARNESS_RUN_ID"           # spawn injects the intent-queue run_id into the child session via env (read by stage_skill)
# The queue for intents staged from a live user session (/learn, or the model acting on its own).
# spawn injects a run id into every child it launches and drains that run when the child exits; a
# user's own session has neither, so before this existed stage_skill refused with "unsafe run id"
# and the shipped learn skill could never land anything. The main session's Stop drains this queue.
INTERACTIVE_RUN_ID = "interactive"
PROJECT_ROOT_ENV = "CODEX_AUTOHARNESS_PROJECT_ROOT"  # same: repo root (where the queue is persisted)

GLOBAL_ROOT_ENV = "CODEX_AUTOHARNESS_GLOBAL_ROOT"
PROPOSAL_SCHEMA = Path(__file__).parent / "proposal.schema.json"
REFLECTOR_TIMEOUT_S = _int_env("CODEX_AUTOHARNESS_TIMEOUT_S", 180)
ENABLED = bool(_int_env("CODEX_AUTOHARNESS_ENABLED", 1, minimum=0))
