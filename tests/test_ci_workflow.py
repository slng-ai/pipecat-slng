"""V14: CI uv invocations share one interpreter + one synced env.

An unpinned `uv run` after `uv sync --python X` rebuilds .venv with the
default interpreter and drops all extras (B1) — so the job must pin
UV_PYTHON and every `uv run` must be `--no-sync`.
"""

import re
from pathlib import Path

CI_YML = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"


def test_v14_job_pins_uv_python():
    text = CI_YML.read_text()
    assert re.search(r"UV_PYTHON:\s*\$\{\{\s*matrix\.python-version\s*\}\}", text), (
        "CI job must set UV_PYTHON to the matrix interpreter (V14)"
    )


def _run_commands():
    text = CI_YML.read_text()
    return re.findall(r"^\s*run:\s*(.+)$", text, flags=re.MULTILINE)


def test_v14_every_uv_run_is_no_sync():
    runs = [c for c in _run_commands() if c.startswith("uv run")]
    assert runs, "expected uv run steps in ci.yml"
    offenders = [r for r in runs if "--no-sync" not in r]
    assert not offenders, (
        f"uv run without --no-sync re-syncs the env (V14): {offenders}"
    )


def test_v14_sync_installs_all_extras_from_lock():
    syncs = [c for c in _run_commands() if c.startswith("uv sync")]
    assert syncs, "expected a uv sync step in ci.yml"
    for sync in syncs:
        assert "--all-extras" in sync, "sync must install extras (examples deps)"
        assert "--locked" in sync, "sync must fail on stale uv.lock (V14)"
