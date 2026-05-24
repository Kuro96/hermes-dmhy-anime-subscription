import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_readiness.py"


def _bytecode_free_env():
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def test_release_readiness_script_runs_offline_checks_without_pytest_recursion():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-pytest"],
        cwd=REPO_ROOT,
        env=_bytecode_free_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert "release readiness: static AST check passed" in result.stdout
    assert "planned qBittorrent submit:" in result.stdout
    assert "planned organizer:" in result.stdout
    assert "invalid config rejected as expected" in result.stdout


def test_release_readiness_script_fails_explicitly_for_invalid_config():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", "fixtures/config/invalid-unsafe-polling.json", "--skip-pytest"],
        cwd=REPO_ROOT,
        env=_bytecode_free_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode != 0
    assert "release readiness: config validation failed" in result.stdout
    assert "polling.interval_minutes" in result.stdout or "at least 10" in result.stdout
