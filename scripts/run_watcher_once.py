from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import jkk_watch  # noqa: E402
from notifiers.slack_notifier import build_slack_payload, notify_listings_to_slack, send_slack_message  # noqa: E402
from services.slack_presenter import (  # noqa: E402
    load_notification_state,
    mark_slack_notified,
    save_notification_state,
    select_slack_notifications,
)
from watch_lifecycle import deep_update, load_watch_rules  # noqa: E402


def load_runtime_rules(rules_file: Path, env_name: str) -> dict:
    """Load public defaults and merge optional private JSON from the environment."""
    rules = load_watch_rules(rules_file)
    raw_overrides = os.environ.get(env_name, "").strip()
    if not raw_overrides:
        return rules
    try:
        overrides = json.loads(raw_overrides)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{env_name} must contain valid JSON: {error}") from error
    if not isinstance(overrides, dict):
        raise SystemExit(f"{env_name} must contain a JSON object.")
    deep_update(rules, overrides)
    return rules


def build_watcher_args(args: argparse.Namespace) -> argparse.Namespace:
    parser = jkk_watch.build_parser()
    watcher_args = parser.parse_args(
        [
            "--data-dir",
            str(args.data_dir),
            "--rules-file",
            str(args.rules_file),
            "--stats-file",
            str(args.stats_file),
            "--include-excluded",
        ]
    )
    watcher_args.rules = load_runtime_rules(args.rules_file, args.rules_json_env)
    watcher_args.target = list(watcher_args.rules.get("high_priority_jkk_names") or [])
    watcher_args.exclude = list(watcher_args.rules.get("jkk_exclude_names") or [])
    watcher_args.ur_target = list(watcher_args.rules.get("high_priority_ur_names") or [])
    return watcher_args


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_test_slack() -> int:
    payload = {
        "text": "House watcher Slack test",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "House watcher Slack test"}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "这是一条测试通知。实际运行时只会推送高优先级 new / reappeared / stable 房源。",
                },
            },
        ],
    }
    ok = send_slack_message(payload)
    print("test_slack_sent=" + str(ok).lower())
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run house watcher once and optionally notify Slack.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT, help="Base directory for watcher data.")
    parser.add_argument(
        "--rules-file",
        type=Path,
        default=PROJECT_ROOT / "config" / "watch_rules.json",
        help="Watcher rules JSON.",
    )
    parser.add_argument(
        "--rules-json-env",
        default="WATCH_RULES_JSON",
        help="Environment variable containing private JSON rule overrides.",
    )
    parser.add_argument(
        "--stats-file",
        type=Path,
        default=PROJECT_ROOT / "watch_stats.json",
        help="Existing stats JSON; updated for continuity.",
    )
    parser.add_argument(
        "--notification-state",
        type=Path,
        default=PROJECT_ROOT / "data" / "last_notifications.json",
        help="Slack notification cooldown JSON.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=PROJECT_ROOT / "data" / "last_run_summary.json",
        help="Last Actions/local run summary JSON.",
    )
    parser.add_argument("--dry-run-slack", action="store_true", help="Print Slack payload instead of sending.")
    parser.add_argument("--no-slack", action="store_true", help="Do not send Slack even when candidates exist.")
    parser.add_argument("--test-slack", action="store_true", help="Send a small Slack test message and exit.")
    args = parser.parse_args()

    if args.test_slack:
        return run_test_slack()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / "data").mkdir(parents=True, exist_ok=True)

    watcher_args = build_watcher_args(args)
    report = jkk_watch.build_check_report(watcher_args)
    jkk_watch.update_stats(report, str(args.stats_file), jkk_watch.DEFAULT_STATS_BUCKET_MINUTES)
    summary = report.lifecycle_summary or {}
    rules = watcher_args.rules

    notification_state = load_notification_state(args.notification_state)
    candidates, skipped = select_slack_notifications(summary, rules, notification_state)

    slack_configured = bool(os.environ.get("SLACK_WEBHOOK_URL"))
    slack_sent = False
    if candidates and not args.no_slack:
        if args.dry_run_slack:
            print(json.dumps(build_slack_payload(candidates, summary), ensure_ascii=False, indent=2))
        else:
            slack_sent = notify_listings_to_slack(candidates, summary)
            if slack_sent:
                notification_state = mark_slack_notified(
                    notification_state,
                    candidates,
                    summary.get("checked_at") or report.checked_at,
                )
    save_notification_state(args.notification_state, notification_state)

    run_summary = {
        "checked_at": report.checked_at,
        "jkk_total": report.total_count,
        "ur_total_vacancies": report.ur_report.total_vacancies if report.ur_report else None,
        "new_events": len(summary.get("new_events") or []),
        "slack_candidates": len(candidates),
        "slack_skipped": len(skipped),
        "slack_configured": slack_configured,
        "slack_sent": slack_sent,
        "dry_run_slack": bool(args.dry_run_slack),
        "candidate_ids": [item.get("stable_id") for item in candidates],
        "skipped": [
            {
                "stable_id": item.get("stable_id"),
                "building_name": item.get("building_name"),
                "event_type": item.get("event_type"),
                "reason": item.get("skip_reason"),
            }
            for item in skipped[:50]
        ],
    }
    write_json(args.summary_file, run_summary)
    print(json.dumps(run_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
