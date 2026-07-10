from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def send_slack_message(
    payload: dict[str, Any],
    webhook_url: str | None = None,
    timeout: int = 15,
) -> bool:
    """Send one Slack Incoming Webhook payload.

    Notification failures are intentionally non-fatal for scheduled watcher runs.
    """
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        print("Slack webhook is not configured; skipping notification.", file=sys.stderr)
        return False

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return True
            print(f"Slack webhook returned HTTP {status}.", file=sys.stderr)
            return False
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"Slack notification failed: {error}", file=sys.stderr)
        return False


def slack_escape(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def listing_line(record: dict[str, Any]) -> str:
    source = record.get("source") or "-"
    building = record.get("building_name") or "-"
    room = record.get("room_no") or "-"
    layout = record.get("layout") or "-"
    area = record.get("area") or "-"
    rent = record.get("rent") or "-"
    fee = record.get("common_fee") or "-"
    status = record.get("current_status") or "-"
    first_seen = record.get("first_seen_at") or "-"
    lifetime = record.get("lifetime_minutes", 0)
    detail = record.get("detail_url") or ""
    prefix = "【重点 UR】 " if source == "UR" and record.get("is_high_priority") else ""
    action = (
        "UR参考房源出现，请关注户型/价格/楼层；如需行动请电话或线下确认"
        if source == "UR"
        else "JKK新房源出现，建议尽快手动登录官网确认"
    )
    lines = [
        f"*{prefix}{slack_escape(action)}*",
        f"来源: {slack_escape(source)} | 団地: {slack_escape(building)} | 房号: {slack_escape(room)}",
        f"户型: {slack_escape(layout)} | 面积: {slack_escape(area)} | 租金: {slack_escape(rent)} | 共益费: {slack_escape(fee)}",
        f"状态: {slack_escape(status)} | 首次: {slack_escape(first_seen)} | 已持续: {slack_escape(lifetime)} 分",
    ]
    if detail:
        lines.append(f"详情: <{slack_escape(detail)}|打开>")
    return "\n".join(lines)


def build_slack_payload(
    listings: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checked_at = (summary or {}).get("checked_at") or "-"
    has_priority_ur = any(item.get("source") == "UR" and item.get("is_high_priority") for item in listings)
    title = "【重点 UR】房源提醒" if has_priority_ur else "房源提醒"
    text = f"{title}: {len(listings)} 件 / {checked_at}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Checked: {slack_escape(checked_at)}"}],
        },
    ]
    for index, record in enumerate(listings[:10]):
        if index:
            blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": listing_line(record)}})
    if len(listings) > 10:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"另有 {len(listings) - 10} 件未展开。"}],
            }
        )
    return {"text": text, "blocks": blocks}


def notify_listings_to_slack(
    listings: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    webhook_url: str | None = None,
) -> bool:
    if not listings:
        return False
    return send_slack_message(build_slack_payload(listings, summary), webhook_url=webhook_url)
