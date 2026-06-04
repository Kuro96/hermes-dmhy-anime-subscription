# Hermes DMHY Anime Subscription

Hermes directory plugin for DMHY public RSS subscriptions. It reads fixture or public RSS feeds, matches releases against subscription rules, plans or submits qBittorrent jobs, tracks state in SQLite, plans safe media organization, and can emit webhook events.

The safe path is dry-run first. Dry-run qBittorrent, organizer, and webhook work is planner-only and does not call live services, copy files, or write configured SQLite state.

## Scope Boundaries

This plugin supports public DMHY RSS feeds only. It does not log in to private trackers, scrape DMHY webpages, call media-server APIs, install a daemon, create cron entries, or depend on sibling plugin repositories.

The runtime package uses the Python standard library only. Tests use `pytest`.

## Quickstart, Dry-Run First

Run these commands from this repository. They create a temporary sandbox config from the checked-in fixture, then run a full dry-run with fixture RSS and a fake completed media file.

```bash
export HERMES_DMHY_SANDBOX="$(mktemp -d)"
mkdir -p "$HERMES_DMHY_SANDBOX/downloads" "$HERMES_DMHY_SANDBOX/library" "$HERMES_DMHY_SANDBOX/staging" "$HERMES_DMHY_SANDBOX/state"
cp fixtures/config/valid.example.json "$HERMES_DMHY_SANDBOX/config.json"
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["HERMES_DMHY_SANDBOX"])
config_path = root / "config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["state"]["path"] = str(root / "state" / "dmhy-subscription.sqlite3")
config["qbittorrent"]["save_path"] = str(root / "downloads")
config["organizer"]["library_root"] = str(root / "library")
config["organizer"]["staging_root"] = str(root / "staging")
config["subscriptions"]["rules"][0]["include_keywords"] = ["Example Anime", "1080p"]
config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
(root / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv").write_bytes(b"fixture video")
print(config_path)
PY
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli validate-config --config "$HERMES_DMHY_SANDBOX/config.json"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli run-once --config "$HERMES_DMHY_SANDBOX/config.json" --dry-run --feed-file fixtures/dmhy/rss-anime.xml --completed-source-path "$HERMES_DMHY_SANDBOX/downloads/[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
```

Expected output includes these lines:

```text
valid config: .../config.json
run once: dry_run=True parsed=1 candidates=1 planned=1
planned qBittorrent submit: ... status=planned ...
planned webhook: ... event_type=download_planned
planned organizer: ... status=planned ... destination=...
planned webhook: ... event_type=download_completed
```

When you are ready to use live services, set qBittorrent credential environment variables, keep webhook URLs in environment variables, change `organizer.mode` only after testing paths, then run apply commands such as `run-once --apply` or `monitor-once --apply`. Apply mode with organization enabled is blocked unless qBittorrent credential env names and values are present and organizer mode is `apply` or `move`.

## Commands

Use the installed script `hermes-dmhy` or run the module directly with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli` during development.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli --help
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli validate-config --config fixtures/config/valid.example.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli run-once --config config.json --dry-run --feed-file fixtures/dmhy/rss-anime.xml
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli run-once --config config.json --dry-run --feed-file fixtures/dmhy/rss-anime.xml --completed-source-path /sandbox/downloads/Example.mkv
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli run-once --config config.json --apply
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli monitor-once --config config.json --snapshot-json snapshots.json --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli monitor-once --config config.json --snapshot-json snapshots.json --apply
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli organize-once --config config.json --job-id job-1 --torrent-hash HASH --title "Example - 01" --source-path /sandbox/download.mkv
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli state --config config.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli failures --config config.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli retry-failed --config config.json --job-id job-1
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli schedule-tick --config config.json --feed-file fixtures/dmhy/rss-anime.xml
```

`schedule-tick` is bounded and exits after one tick. By default it remains a safe dry-run and only plans RSS matching/submission. For a real production scheduler, call it with `--apply` after validating the config and environment:

```bash
# Example cron/no-agent command; keep secrets in environment variables, not config.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m hermes_dmhy_anime_subscription.cli schedule-tick --config /path/to/config.json --apply
```

`--apply` performs the complete production tick: list all qBittorrent torrents so pre-existing active jobs are not missed after category changes, match those pre-existing active jobs to torrent state (including base32 RSS infohash to qBittorrent hex hash conversion), monitor completed downloads and run the organizer according to config, then submit newly matched RSS items to qBittorrent. It prints a JSON summary suitable for scheduler logs. Apply mode is still guarded by `ensure_apply_safe`: qBittorrent credential env var names and values must exist, webhook URL env values must exist when enabled, Telegram bot token env values must exist when enabled, and `organizer.mode` must be `apply` or `move`.

The plugin does not install a service or cron job; use your own scheduler to call the bounded command at the configured interval.

## Config Reference

The config file is JSON. Relative paths are resolved from the config file directory.

### `dmhy`

`dmhy.feeds` is a non-empty list of RSS sources.

```json
{
  "name": "dmhy-main",
  "url": "https://share.dmhy.org/topics/rss/rss.xml"
}
```

`name` is used by subscription rules. `url` can be the base public DMHY RSS URL or a public RSS URL with DMHY query parameters.

### `subscriptions.rules`

Each rule is matched in order. The first accepted rule creates the candidate.

`name` is required and appears in job metadata and webhook payloads.

`include_keywords` is a list of required title terms. With `use_regex: false`, each value is matched as plain text. With `use_regex: true`, values are regex patterns.

`exclude_keywords` rejects matching releases.

`use_regex` changes include and exclude keyword matching from plain text to regex.

`team_names` limits releases by release group or author text when present.

`resolutions` limits releases by quality tokens such as `1080p`.

`categories` limits releases by feed category values such as `動畫`.

`episode_mode` is `episode`, `pack`, or `both`.

`allow_packs` must be `true` for season packs unless `episode_mode` already accepts packs.

`feed_names` limits a rule to named feeds. Leave it empty to accept any configured feed.

`save_path` overrides `qbittorrent.save_path` for this rule.

`category` overrides `qbittorrent.category` for this rule.

`priority` is stored on the release candidate.

`bangumi_subject_id` is optional. When set, apply-mode monitoring can archive the rule after Bangumi reports the subject's main episodes and the corresponding downloaded episodes are completed and organized.

`enabled` defaults to `true`. Set it to `false` to keep a rule in the file without matching it.

### `qbittorrent`

`endpoint` is the qBittorrent Web UI base URL, for example `http://127.0.0.1:8080`.

`username_env` and `password_env` are environment variable names, not secret values. Apply mode requires both names and both environment variables to be set.

`category` is sent to qBittorrent unless a rule overrides it.

`tags` is a list sent as comma-separated qBittorrent tags.

`save_path` is sent as the qBittorrent save path unless a rule overrides it. Use a sandbox path while testing.

Dry-run qBittorrent submission prints the planned payload, makes no HTTP calls, and does not mark feed items as seen in configured state.

### `polling`

`interval_minutes` must be at least `10`.

`jitter_seconds` must be zero or greater. It is guidance for the external scheduler and is printed by `validate-config`.

### `state`

`path` is the SQLite file used for seen feed items, submitted jobs, retry records, failures, organizer outcomes, and archived subscription rules during apply and stateful monitor operations. Dry-run planning uses ephemeral in-memory state for any planned changes, while reading selected existing tables from this file in read-only mode for accurate previews.

Dry-run `run-once` and `schedule-tick` do not initialize or migrate the configured SQLite file. If the file already has `archived_rules` or `satisfied_season_packs` tables, they read those tables in read-only mode to skip archived rules and preserve completed-pack suppressions in previews; a missing file or missing table is treated as no archived rules or satisfied packs.

Archived rules are created only by apply-mode monitoring after a rule with `bangumi_subject_id` has all Bangumi main episodes completed and organized. Once archived, the rule stays in state history and is skipped by future matching; the `state` command includes archived rules in its JSON output.

### `organizer`

`mode` is `dry-run`, `apply`, or the legacy alias `move`. CLI dry-runs force planning even if the config says `apply` or `move`; production organizer runs copy completed media into `library_root` and leave the qBittorrent source files in place for seeding and rechecks.

`library_root` is the media library destination root. It is required for `apply` and `move` modes.

`staging_root` is reserved for staging workflows and must be a valid path.

### `webhook`

`enabled` turns webhook delivery on or off.

`url_env` is the environment variable name that contains the webhook URL. Do not put the URL in the config file. Apply mode requires this env var to be set when webhook delivery is enabled.

### `telegram`

`enabled` turns Telegram episode update delivery on or off. Telegram delivery runs only during non-dry-run monitoring after the organizer successfully applies a video episode action, so download submission and dry-run planning do not send chat messages.

`bot_token_env` is the environment variable name that contains the Telegram bot token. Do not put the bot token in the config file; literal token-shaped values are rejected. Apply mode requires this env var to be set when Telegram delivery is enabled.

`chat_id` is the target Telegram chat/channel ID. `message_thread_id` is optional for forum topics. `parse_mode` defaults to `Markdown`, and `timeout` optionally overrides the Telegram HTTP timeout in seconds.

Telegram photo notifications use the rule's `bangumi_subject_id` to fetch the Bangumi subject cover URL. If a completed episode has no configured Bangumi subject ID, no Telegram episode update is sent.

Example:

```json
{
  "enabled": true,
  "bot_token_env": "TELEGRAM_BOT_TOKEN",
  "chat_id": "-1001234567890",
  "message_thread_id": 22274,
  "parse_mode": "Markdown"
}
```

### `retry`

`max_attempts` is the number of monitor failures allowed before a job is marked failed. It must be at least `1`.

`backoff_seconds` is the delay stored for the next retry after a retryable failure. It must be zero or greater.

## qBittorrent Setup

Enable the qBittorrent Web UI, choose an endpoint, then store credentials in environment variables named by the config.

```bash
export QBITTORRENT_USERNAME="your-user"
export QBITTORRENT_PASSWORD="your-password"
```

Dry-runs don't use these values. Apply mode logs in with `/api/v2/auth/login`, then posts to `/api/v2/torrents/add` with the magnet or torrent URL, category, tags, and save path.

If qBittorrent reports the torrent is already present, the plugin treats that as an idempotent success.

## Media Organization Policy

Organizer output targets a Jellyfin, Plex, and Emby compatible layout:

```text
Library Root/
  Series Title/
    Season 01/
      Series Title - S01E01 - ReleaseGroup [1080p].mkv
      Series Title - S01E01 - ReleaseGroup [1080p].ass
```

If the episode cannot be parsed, the planned destination goes under `_Unsorted/Series Title/` and the action status is `unsorted`.

The organizer never deletes files. It refuses to overwrite existing destinations. Destination paths must stay contained under `library_root`. Sample, extras, trailer, NCOP, and NCED videos are filtered out. Subtitles with `.ass`, `.srt`, `.ssa`, or `.vtt` are preserved when they match the selected video stem or live beside the selected video.

## Webhook Payload Example

Webhook payloads are JSON. Disabled webhooks are still planned in dry-run output.

```json
{
  "event_type": "download_completed",
  "subscription": {
    "rule_id": "example-show",
    "rule_name": "example-show"
  },
  "release": {
    "title": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
    "guid": "https://share.dmhy.org/topics/view/100001_example_anime_01.html",
    "infohash": "ABCDEF1234567890ABCDEF1234567890ABCDEF12"
  },
  "qbittorrent": {
    "job_id": "dmhy-abcdef1234567890abcdef1234567890abcdef12",
    "hash": "ABCDEF1234567890ABCDEF1234567890ABCDEF12"
  },
  "status": "completed",
  "failure_reason": null,
  "timestamp": "2026-05-24T10:30:00+00:00",
  "dry_run": true,
  "severity": "info",
  "title": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
  "message": "Download completed"
}
```

## Troubleshooting

Duplicate releases: the state database dedupes by infohash first, then guid, then title and publish date. If the same release appears in several feeds, only the first accepted item creates a job.

Season packs: use `episode_mode: "pack"` or `episode_mode: "both"`, and set `allow_packs: true` when you expect pack releases. Keep episode-only rules on `episode` to avoid full-season downloads.

qBittorrent auth failure: check that `qbittorrent.username_env` and `qbittorrent.password_env` are environment variable names, that both variables are set, and that the Web UI endpoint is reachable from the process.

Organizer collision: a destination file already exists. The plugin reports a conflict and does not overwrite. Rename or remove the existing destination yourself before retrying.

Webhook failure: when enabled, the URL must come from `webhook.url_env`. Retryable HTTP and transport failures are recorded as failures. Dry-run only prints planned delivery.

Retry exhaustion: stalled, error, missing, or deleted torrent states increase retry counts. When `retry.max_attempts` is reached, the job is marked failed and appears in `failures`. Use `retry-failed --job-id ...` after fixing the cause.

## Release Readiness

Operators and CI can run the release-readiness script without live DMHY, qBittorrent, webhook, or media-server calls.

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/release_readiness.py
```

The script runs repo-native pytest, parses all Python source and test files as a static check, validates the fixture config, runs the fixture e2e dry-run quickstart path in a temp sandbox, and confirms an invalid config fixture is rejected.

To prove invalid config failure explicitly:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/release_readiness.py --config fixtures/config/invalid-unsafe-polling.json --skip-pytest
```

That command exits non-zero and prints the config validation error.

Optional live integration checks are manual and skipped by default. If you run them, use a disposable qBittorrent category and save path, set credential env vars, set the webhook URL env var if enabled, and run `run-once --apply` only after the dry-run output is correct.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider
```

Keep `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` in local verification to avoid `__pycache__` and `.pytest_cache` artifacts.

## Hermes Contract Source

This repository follows the Hermes directory plugin contract: a plugin directory with `plugin.yaml` plus Python code exposing `def register(ctx): ...`. Registration tolerates Hermes contexts that expose only a subset of `register_tool`, `register_hook`, `register_cli_command`, or `register_command`.
