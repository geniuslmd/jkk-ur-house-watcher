# JKK / UR House Watcher

[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

A lightweight watcher for public JKK and UR rental listings in Tokyo. It runs on GitHub Actions, records listing lifecycle changes, and sends only actionable notifications to Slack.

It does not sign in to JKK/UR or submit applications automatically.

## How It Works

- Checks JKK and UR public listing pages every 10 minutes, every day, from 09:00 to 18:50 JST.
- Sends one compact daily Slack heartbeat after the 09:00 JST check.
- Keeps the other checks quiet unless a priority listing matches the notification rules.
- Tracks `new`, `stable`, `reappeared`, and fast-disappearing listings between runs.

GitHub Actions schedules can be delayed by a few minutes, so this is not an exact real-time scheduler.

## Private Rules

The public configuration intentionally has no personal target listings. Configure your own targets through the `WATCH_RULES_JSON` GitHub Actions repository secret:

```json
{
  "high_priority_ur_names": ["Your UR target"],
  "high_priority_jkk_names": ["Your JKK target A", "Your JKK target B"],
  "jkk_exclude_names": ["Optional excluded JKK building"],
  "slack_notifications": {
    "priority_min_layout_bedrooms": 2
  }
}
```

With `priority_min_layout_bedrooms: 2`, layouts such as `2LDK`, `2SLDK`, `3LDK`, and `4LDK` qualify; `1LDK` does not.

## GitHub Actions Setup

1. Fork or create a repository with this code.
2. Open `Settings -> Secrets and variables -> Actions -> New repository secret`.
3. Add `SLACK_WEBHOOK_URL` with your Slack Incoming Webhook URL.
4. Add `WATCH_RULES_JSON` with your private rule JSON.
5. Open `Settings -> Actions -> General -> Workflow permissions` and select `Read and write permissions`.
6. Open `Actions -> House Watcher -> Run workflow`.
   - Check `Only send a Slack test message` first to verify Slack.
   - Run again without that checkbox to make a real one-time query.

The workflow file is [`.github/workflows/house_watcher.yml`](.github/workflows/house_watcher.yml).

## Local One-Time Run

The project only uses the Python standard library.

```powershell
$env:SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."
$env:WATCH_RULES_JSON = '{"high_priority_ur_names":["Your UR target"],"high_priority_jkk_names":["Your JKK target"],"slack_notifications":{"priority_min_layout_bedrooms":2}}'
python scripts/run_watcher_once.py --dry-run-slack
```

Use `--test-slack` to send a small Slack test message without fetching listing pages.

## Privacy

Do not commit `SLACK_WEBHOOK_URL` or `WATCH_RULES_JSON`. They are intended for GitHub Actions secrets or your local environment only.

GitHub Actions deliberately commits limited state JSON files so later runs can detect new and reappeared listings. If the repository is public, that listing history is public too. Screenshots, raw evidence, local logs, and secrets are excluded by `.gitignore`.

## Notes

- Page layouts and access restrictions can change, requiring scraper maintenance.
- UR results are reference information; confirm details with the official site, phone, or in person before acting.
- Use a reasonable refresh interval and follow the target websites' rules.
