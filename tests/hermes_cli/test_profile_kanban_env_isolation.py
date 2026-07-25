"""Cross-profile isolation for dispatcher-spawned Kanban worker authority."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
_KANBAN_ENV_PREFIX = "HERMES_KANBAN_"


def _fresh_profile_process(
    tmp_path: Path,
    *,
    current_profile: str,
    selected_profile: str,
) -> dict[str, str]:
    """Import the real CLI bootstrap in a fresh process and report its env."""
    hermes_root = tmp_path / ".hermes"
    current_home = hermes_root / "profiles" / current_profile
    selected_home = hermes_root / "profiles" / selected_profile
    current_home.mkdir(parents=True)
    selected_home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(current_home),
            "HERMES_PROFILE": current_profile,
            "HERMES_KANBAN_TASK": "t_parent",
            "HERMES_KANBAN_RUN_ID": "17",
            "HERMES_KANBAN_CLAIM_LOCK": "claim-parent",
            "HERMES_KANBAN_BOARD": "parent-board",
            "HERMES_KANBAN_DB": str(tmp_path / "parent-kanban.db"),
            "HERMES_KANBAN_WORKSPACE": str(tmp_path / "parent-workspace"),
            "HERMES_KANBAN_GOAL_MODE": "1",
        }
    )
    code = """
import json
import os
import sys

sys.argv = ["hermes", "-p", sys.argv[1], "version"]
import hermes_cli.main  # noqa: F401
print("PROFILE_ENV=" + json.dumps({
    key: value
    for key, value in os.environ.items()
    if key == "HERMES_HOME" or key.startswith("HERMES_KANBAN_")
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, selected_profile],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = next(
        line.removeprefix("PROFILE_ENV=")
        for line in result.stdout.splitlines()
        if line.startswith("PROFILE_ENV=")
    )
    observed = json.loads(payload)
    assert observed["HERMES_HOME"] == str(selected_home)
    return observed


def test_cross_profile_cli_drops_inherited_kanban_worker_authority(tmp_path):
    observed = _fresh_profile_process(
        tmp_path,
        current_profile="klerik",
        selected_profile="stackr",
    )

    assert not any(key.startswith(_KANBAN_ENV_PREFIX) for key in observed), observed


def test_dispatcher_assigned_profile_bootstrap_retains_worker_authority(tmp_path):
    observed = _fresh_profile_process(
        tmp_path,
        current_profile="stackr",
        selected_profile="stackr",
    )

    assert observed["HERMES_KANBAN_TASK"] == "t_parent"
    assert observed["HERMES_KANBAN_RUN_ID"] == "17"
    assert observed["HERMES_KANBAN_CLAIM_LOCK"] == "claim-parent"
    assert observed["HERMES_KANBAN_BOARD"] == "parent-board"


def test_same_profile_cli_lifecycle_keeps_current_task_scope(tmp_path):
    observed = _fresh_profile_process(
        tmp_path,
        current_profile="klerik",
        selected_profile="klerik",
    )

    assert observed["HERMES_KANBAN_TASK"] == "t_parent"
    assert observed["HERMES_KANBAN_DB"] == str(tmp_path / "parent-kanban.db")
    assert observed["HERMES_KANBAN_WORKSPACE"] == str(tmp_path / "parent-workspace")
    assert observed["HERMES_KANBAN_GOAL_MODE"] == "1"
