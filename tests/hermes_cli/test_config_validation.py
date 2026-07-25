"""Tests for config.yaml structure validation (validate_config_structure)."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _EXTRA_KNOWN_ROOT_KEYS,
    _KNOWN_ROOT_KEYS,
    validate_config_structure,
    ConfigIssue,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_config_check(hermes_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env.pop("HERMES_PROFILE", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_REPO_ROOT), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "config", "check"],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize(
    ("config_yaml", "expected_path"),
    [
        ("toolsets:\n  '0': credential-value-that-must-not-appear\n", "toolsets"),
        (
            "platform_toolsets:\n"
            "  cli:\n"
            "    '0': credential-value-that-must-not-appear\n",
            "platform_toolsets.cli",
        ),
    ],
)
def test_config_check_rejects_malformed_toolset_shapes_without_echoing_values(
    tmp_path, config_yaml, expected_path
):
    (tmp_path / "config.yaml").write_text(config_yaml, encoding="utf-8")

    result = _run_config_check(tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert expected_path in output
    assert "list" in output
    assert "credential-value-that-must-not-appear" not in output


def test_config_check_keeps_well_formed_toolset_shapes_successful(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "toolsets:\n"
        "  - kanban\n"
        "platform_toolsets:\n"
        "  cli:\n"
        "    - kanban\n",
        encoding="utf-8",
    )

    result = _run_config_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1


    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)




class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""


    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b


class TestUnknownTopLevelKeys:
    """Arbitrary top-level keys must NOT warn — they are bridged to os.environ.

    Top-level scalars in config.yaml are forwarded into the environment
    (gateway/run.py, hermes send) so users can feed skills and external apps
    env-style keys like DISCORD_HOME_CHANNEL or MY_APP_TOKEN. A closed-world
    allowlist can never enumerate those, so no "Unknown top-level config key"
    warning may exist.
    """


    def test_known_root_keys_derived_from_default_config(self):
        """_KNOWN_ROOT_KEYS must be DEFAULT_CONFIG.keys() plus extras — single source of truth."""
        assert set(DEFAULT_CONFIG.keys()).issubset(_KNOWN_ROOT_KEYS)
        assert _EXTRA_KNOWN_ROOT_KEYS.issubset(_KNOWN_ROOT_KEYS)
        assert _KNOWN_ROOT_KEYS == frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

    def test_provider_like_unknown_root_keeps_misplaced_message(self):
        """Preserve existing base_url/api_key root-level guidance."""
        issues = validate_config_structure({
            "base_url": "https://example.com/v1",
            "api_key": "secret",
        })
        misplaced = [
            i for i in issues
            if i.severity == "warning" and "looks misplaced" in i.message
        ]
        assert any("base_url" in i.message for i in misplaced)
        assert any("api_key" in i.message for i in misplaced)

