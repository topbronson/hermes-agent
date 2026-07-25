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
    current_home_alias: bool = False,
) -> dict[str, str]:
    """Import the real CLI bootstrap in a fresh process and report its env."""
    hermes_root = tmp_path / ".hermes"
    current_home = hermes_root / "profiles" / current_profile
    selected_home = hermes_root / "profiles" / selected_profile
    current_home.mkdir(parents=True)
    selected_home.mkdir(parents=True, exist_ok=True)
    inherited_home = current_home
    if current_home_alias:
        inherited_home = tmp_path / "current-profile-home"
        inherited_home.symlink_to(current_home, target_is_directory=True)

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(inherited_home),
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
import importlib.abc
import json
import os
import sys


class AuthorityImportSentinel(importlib.abc.MetaPathFinder):
    observed = None

    def find_spec(self, fullname, path, target=None):
        if self.observed is None and (
            fullname == "hermes_cli.profiles"
            or fullname.startswith("hermes_cli.subcommands.")
        ):
            self.observed = {
                key: value
                for key, value in os.environ.items()
                if key.startswith("HERMES_KANBAN_")
            }
        return None


sentinel = AuthorityImportSentinel()
sys.meta_path.insert(0, sentinel)
sys.argv = ["hermes", "-p", sys.argv[1], "version"]
import hermes_cli.main  # noqa: F401
print("PROFILE_IMPORT_ENV=" + json.dumps(sentinel.observed, sort_keys=True))
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
    import_payload = next(
        line.removeprefix("PROFILE_IMPORT_ENV=")
        for line in result.stdout.splitlines()
        if line.startswith("PROFILE_IMPORT_ENV=")
    )
    observed = json.loads(payload)
    observed["first_authority_sensitive_import"] = json.loads(import_payload)
    assert observed["HERMES_HOME"] == str(selected_home)
    return observed


def test_cross_profile_cli_scrubs_authority_before_profile_or_subcommand_import(tmp_path):
    observed = _fresh_profile_process(
        tmp_path,
        current_profile="klerik",
        selected_profile="stackr",
    )

    assert observed["first_authority_sensitive_import"] == {}


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


def test_same_profile_symlink_home_keeps_current_task_scope(tmp_path):
    observed = _fresh_profile_process(
        tmp_path,
        current_profile="stackr",
        selected_profile="stackr",
        current_home_alias=True,
    )

    assert observed["HERMES_KANBAN_TASK"] == "t_parent"
    assert observed["HERMES_KANBAN_RUN_ID"] == "17"
    assert observed["HERMES_KANBAN_CLAIM_LOCK"] == "claim-parent"
    assert observed["HERMES_KANBAN_BOARD"] == "parent-board"
