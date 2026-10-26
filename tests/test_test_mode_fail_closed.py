"""Regression tests for fail-closed production authentication configuration."""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_auth_with_env(bookq_env: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["BOOKQ_ENV"] = bookq_env
    return subprocess.run(
        [sys.executable, "-c", "import app.auth"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_test_mode_is_rejected_in_production():
    result = _import_auth_with_env("production")

    assert result.returncode != 0
    assert "BOOKQ_TEST_MODE=true is forbidden" in result.stderr


def test_test_mode_remains_available_in_development():
    result = _import_auth_with_env("development")

    assert result.returncode == 0, result.stderr
