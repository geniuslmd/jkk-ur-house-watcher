from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_SLACK_RULES = {
    "enabled": True,
    "cooldown_minutes": 30,
    "notify_event_types": ["first_seen", "reappeared", "stable"],
    "notify_disappeared": False,
    "notify_jkk_newer_buildings": False,
    "notify_ordinary_ur": False,
    "notify_ordinary_jkk": False,
    "allowed_layouts": [],
    "priority_min_layout_bedrooms": 2,
}


def slack_rules(watch_rules: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_SLACK_RULES)
    merged.update(watch_rules.get("slack_notifications") or {})
    return merged


def load_notification_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notification_state(path: str | Path, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def record_by_id(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in summary.get("records") or []:
        stable_id = record.get("stable_id")
        if stable_id:
            records[stable_id] = record
    return records


def is_named_priority(record: dict[str, Any], watch_rules: dict[str, Any]) -> bool:
    source = record.get("source")
    name = str(record.get("building_name") or "")
    if source == "UR":
        return any(target in name for target in watch_rules.get("high_priority_ur_names", []))
    if source == "JKK":
        return any(target in name for target in watch_rules.get("high_priority_jkk_names", []))
    return False


def is_newer_jkk(record: dict[str, Any]) -> bool:
    raw = record.get("raw") or {}
    return bool(raw.get("is_newer_building"))


def layout_bedrooms(layout: object) -> int | None:
    """Return the leading bedroom count for layouts such as 2LDK or 3SLDK."""
    normalized = unicodedata.normalize("NFKC", str(layout or "")).upper()
    match = re.search(r"(\d+)\s*(?:S?LDK)", normalized)
    return int(match.group(1)) if match else None


def passes_priority_layout_rule(record: dict[str, Any], notify_rules: dict[str, Any]) -> bool:
    minimum = notify_rules.get("priority_min_layout_bedrooms")
    if minimum in (None, ""):
        return True
    bedrooms = layout_bedrooms(record.get("layout"))
    return bedrooms is not None and bedrooms >= int(minimum)


def event_to_record(
    event: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stable_id = event.get("stable_id")
    record = dict(records.get(stable_id) or {})
    for key in [
        "stable_id",
        "source",
        "building_name",
        "room_no",
        "layout",
        "area",
        "rent",
        "common_fee",
        "current_status",
        "first_seen_at",
        "last_seen_at",
        "lifetime_minutes",
        "appearance_count",
        "is_high_priority",
    ]:
        if key not in record or record.get(key) in (None, ""):
            record[key] = event.get(key)
    record["event_type"] = event.get("event_type")
    return record


def is_actionable_record(
    record: dict[str, Any],
    event: dict[str, Any],
    watch_rules: dict[str, Any],
    notify_rules: dict[str, Any],
) -> bool:
    event_type = event.get("event_type")
    if event_type in {"disappeared", "disappeared_fast"}:
        return bool(notify_rules.get("notify_disappeared"))
    if event_type not in set(notify_rules.get("notify_event_types") or []):
        return False
    allowed_layouts = [str(item) for item in notify_rules.get("allowed_layouts") or [] if str(item)]
    if allowed_layouts:
        layout = str(record.get("layout") or "")
        if not any(allowed in layout for allowed in allowed_layouts):
            return False

    if is_named_priority(record, watch_rules):
        if not passes_priority_layout_rule(record, notify_rules):
            return False
        return True
    if record.get("source") == "JKK" and notify_rules.get("notify_jkk_newer_buildings") and is_newer_jkk(record):
        return True
    if record.get("source") == "UR" and notify_rules.get("notify_ordinary_ur"):
        return True
    if record.get("source") == "JKK" and notify_rules.get("notify_ordinary_jkk"):
        return True
    return False


def passes_cooldown(
    record: dict[str, Any],
    notification_state: dict[str, Any],
    checked_at: str,
    cooldown_minutes: int,
) -> bool:
    stable_id = record.get("stable_id")
    if not stable_id:
        return False
    previous = notification_state.get(stable_id) or {}
    current_status = record.get("current_status")
    if previous.get("last_status") != current_status:
        return True
    last_sent = parse_time(previous.get("last_sent_at"))
    checked = parse_time(checked_at) or datetime.now()
    if not last_sent:
        return True
    return checked - last_sent >= timedelta(minutes=cooldown_minutes)


def select_slack_notifications(
    lifecycle_summary: dict[str, Any],
    watch_rules: dict[str, Any],
    notification_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    notify_rules = slack_rules(watch_rules)
    if not notify_rules.get("enabled", True):
        return [], []

    records = record_by_id(lifecycle_summary)
    checked_at = lifecycle_summary.get("checked_at") or datetime.now().isoformat(timespec="seconds")
    cooldown_minutes = int(notify_rules.get("cooldown_minutes", 30))
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for event in lifecycle_summary.get("new_events") or []:
        record = event_to_record(event, records)
        stable_id = record.get("stable_id")
        if not stable_id or stable_id in seen_ids:
            continue
        seen_ids.add(stable_id)
        if not is_actionable_record(record, event, watch_rules, notify_rules):
            skipped.append({**record, "skip_reason": "not_actionable"})
            continue
        if not passes_cooldown(record, notification_state, checked_at, cooldown_minutes):
            skipped.append({**record, "skip_reason": "cooldown"})
            continue
        selected.append(record)
    return selected, skipped


def mark_slack_notified(
    notification_state: dict[str, Any],
    records: list[dict[str, Any]],
    checked_at: str,
) -> dict[str, Any]:
    updated = dict(notification_state)
    for record in records:
        stable_id = record.get("stable_id")
        if not stable_id:
            continue
        updated[stable_id] = {
            "last_sent_at": checked_at,
            "last_status": record.get("current_status"),
            "last_event_type": record.get("event_type"),
            "source": record.get("source"),
            "building_name": record.get("building_name"),
            "room_no": record.get("room_no"),
        }
    return updated
