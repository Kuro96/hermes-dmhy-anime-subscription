#!/usr/bin/env python3
"""Run release-readiness checks without live external services."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_CONFIG = REPO_ROOT / "fixtures" / "config" / "valid.example.json"
INVALID_CONFIG = REPO_ROOT / "fixtures" / "config" / "invalid-unsafe-polling.json"
FIXTURE_RSS = REPO_ROOT / "fixtures" / "dmhy" / "rss-anime.xml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline release-readiness checks for the DMHY plugin")
    parser.add_argument("--config", default=str(VALID_CONFIG), help="config file to validate before readiness checks")
    parser.add_argument("--skip-pytest", action="store_true", help="skip pytest, useful when checking invalid config failure")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    config_path = (REPO_ROOT / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    print(f"release readiness: validating {config_path}")
    validate_result = _run_cli(["validate-config", "--config", str(config_path)], env=env)
    if validate_result.returncode != 0:
        _print_result(validate_result)
        print("release readiness: config validation failed")
        return validate_result.returncode
    _print_result(validate_result)

    if not args.skip_pytest:
        pytest_result = _run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], env=env)
        _print_result(pytest_result)
        if pytest_result.returncode != 0:
            return pytest_result.returncode

    static_error = _static_ast_check()
    if static_error is not None:
        print(static_error)
        return 1
    print("release readiness: static AST check passed")

    dry_run_result = _fixture_dry_run(env)
    _print_result(dry_run_result)
    if dry_run_result.returncode != 0:
        return dry_run_result.returncode
    required_markers = (
        "planned qBittorrent submit:",
        "planned webhook:",
        "planned organizer:",
        "event_type=download_completed",
    )
    missing = [marker for marker in required_markers if marker not in dry_run_result.stdout]
    if missing:
        print(f"release readiness: dry-run output missing markers: {', '.join(missing)}")
        return 1

    invalid_result = _run_cli(["validate-config", "--config", str(INVALID_CONFIG)], env=env)
    if invalid_result.returncode == 0:
        print(f"release readiness: invalid config was accepted: {INVALID_CONFIG}")
        return 1
    print(f"release readiness: invalid config rejected as expected: {INVALID_CONFIG.name}")
    print("release readiness: passed")
    return 0


def _run(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def _run_cli(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "hermes_dmhy_anime_subscription.cli", *args], env=env)


def _print_result(result: subprocess.CompletedProcess[str]) -> None:
    command = " ".join(result.args) if isinstance(result.args, list) else str(result.args)
    print(f"$ {command}")
    print(result.stdout.rstrip())


def _static_ast_check() -> str | None:
    for directory in (REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                return f"release readiness: static AST check failed for {path}: {exc}"
    return None


def _fixture_dry_run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="hermes-dmhy-readiness-") as temp_dir:
        root = Path(temp_dir)
        downloads = root / "downloads"
        downloads.mkdir()
        source = downloads / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
        source.write_bytes(b"fixture video")
        config = json.loads(VALID_CONFIG.read_text(encoding="utf-8"))
        config["state"]["path"] = str(root / "state.sqlite3")
        config["qbittorrent"]["save_path"] = str(downloads)
        config["organizer"]["library_root"] = str(root / "library")
        config["organizer"]["staging_root"] = str(root / "staging")
        config["subscriptions"]["rules"][0]["include_keywords"] = ["Example Anime", "1080p"]
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return _run_cli(
            [
                "run-once",
                "--config",
                str(config_path),
                "--dry-run",
                "--feed-file",
                str(FIXTURE_RSS),
                "--completed-source-path",
                str(source),
            ],
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
