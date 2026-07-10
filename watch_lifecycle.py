# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_RULES = {
    "normal_interval_seconds": 600,
    "hot_interval_seconds": 300,
    "event_boost_interval_seconds": 60,
    "event_boost_duration_minutes": 20,
    "fast_disappear_minutes": 15,
    "stable_seen_checks": 3,
    "popup_cooldown_minutes": 30,
    "fetch_lock_stale_seconds": 300,
    "high_priority_ur_names": ["ヌーヴェル赤羽台"],
    "high_priority_jkk_names": ["コーシャハイム加賀", "コーシャハイム田端テラス"],
    "quick_judgement": {
        "low_rent_yen": 100000,
        "high_rent_yen": 200000,
        "small_area_m2": 40,
        "large_area_m2": 60,
    },
}


def load_watch_rules(path: str | Path | None) -> dict[str, Any]:
    rules = json.loads(json.dumps(DEFAULT_RULES, ensure_ascii=False))
    if path:
        config_path = Path(path)
        if config_path.exists():
            try:
                user_rules = json.loads(config_path.read_text(encoding="utf-8"))
                deep_update(rules, user_rules)
            except Exception:
                pass
    return rules


def deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def minutes_between(start: str | None, end: str | None) -> float:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if not start_dt or not end_dt:
        return 0.0
    return round(max(0.0, (end_dt - start_dt).total_seconds() / 60), 1)


def make_stable_id(record: dict[str, Any]) -> str:
    official = str(record.get("official_id") or "").strip()
    if official:
        raw = f"{record.get('source', '')}|official|{official}"
    else:
        raw = "|".join(
            normalize_key_part(record.get(key, ""))
            for key in [
                "source",
                "building_name",
                "room_no",
                "layout",
                "area",
                "rent",
            ]
        )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{normalize_key_part(record.get('source', 'listing')).lower()}_{digest}"


def normalize_key_part(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def parse_money(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else None


def parse_area(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else None


class ListingLifecycleStore:
    def __init__(self, base_dir: str | Path, rules: dict[str, Any]) -> None:
        self.base_dir = Path(base_dir)
        self.rules = rules
        self.data_dir = self.base_dir / "data"
        self.snapshots_dir = self.data_dir / "snapshots"
        self.evidence_dir = self.data_dir / "evidence"
        self.listings_file = self.data_dir / "listings.json"
        self.events_file = self.data_dir / "listing_events.json"
        self.meta_file = self.data_dir / "lifecycle_meta.json"
        self.alerts_file = self.data_dir / "notification_history.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def update(
        self,
        current_records: list[dict[str, Any]],
        snapshot_meta: dict[str, Any],
    ) -> dict[str, Any]:
        checked_at = snapshot_meta.get("checked_at") or now_iso()
        listings = self._load_json(self.listings_file, {})
        events = self._load_json(self.events_file, [])
        meta = self._load_json(self.meta_file, {})
        alerts = self._load_json(self.alerts_file, {})
        snapshot_id = self._save_snapshot(checked_at, current_records, snapshot_meta)

        normalized = []
        for record in current_records:
            item = dict(record)
            item["stable_id"] = item.get("stable_id") or make_stable_id(item)
            item["is_high_priority"] = bool(
                item.get("is_high_priority") or self._is_high_priority(item)
            )
            item["quick_tags"] = self._quick_tags(item, None)
            normalized.append(item)

        present_ids = {item["stable_id"] for item in normalized}
        new_events: list[dict[str, Any]] = []
        alert_records: list[dict[str, Any]] = []
        event_triggered_boost = False
        boost_scopes: set[str] = set()

        for item in normalized:
            stable_id = item["stable_id"]
            previous = listings.get(stable_id)
            if previous is None:
                state = self._new_state(item, checked_at, snapshot_id)
                listings[stable_id] = state
                new_events.append(self._event("first_seen", state, checked_at, snapshot_id))
                self._save_evidence(state, checked_at, snapshot_id, "first_seen")
                event_triggered_boost = True
                boost_scopes.add(self._boost_scope(state))
            elif previous.get("is_present"):
                state = self._still_seen(previous, item, checked_at, snapshot_id)
                listings[stable_id] = state
                if state.get("current_status") == "stable" and previous.get("current_status") != "stable":
                    new_events.append(self._event("stable", state, checked_at, snapshot_id))
                else:
                    new_events.append(self._event("still_seen", state, checked_at, snapshot_id))
            else:
                state = self._reappeared(previous, item, checked_at, snapshot_id)
                listings[stable_id] = state
                new_events.append(self._event("reappeared", state, checked_at, snapshot_id))
                self._save_evidence(state, checked_at, snapshot_id, "reappeared")
                event_triggered_boost = True
                boost_scopes.add(self._boost_scope(state))

            state = listings[stable_id]
            state["quick_tags"] = self._quick_tags(item, state)
            if self._should_alert(state, alerts, checked_at):
                alert_records.append(state)

        for stable_id, previous in list(listings.items()):
            if stable_id in present_ids or not previous.get("is_present"):
                continue
            state, event_type = self._disappeared(previous, checked_at, snapshot_id)
            listings[stable_id] = state
            new_events.append(self._event(event_type, state, checked_at, snapshot_id))

        ur_total = int(snapshot_meta.get("ur_total_vacancies") or 0)
        previous_ur_total = int(meta.get("last_ur_total_vacancies") or 0)
        if ur_total > previous_ur_total:
            event_triggered_boost = True
            boost_scopes.add("UR:北区总空室上升")

        if event_triggered_boost:
            boost_until = (
                datetime.fromisoformat(checked_at)
                + timedelta(minutes=int(self.rules.get("event_boost_duration_minutes", 20)))
            ).isoformat(timespec="seconds")
            meta["boost_until"] = boost_until
            meta["boost_reason"] = "new_or_increased_vacancy"
            meta["boost_scopes"] = sorted(scope for scope in boost_scopes if scope)
        meta["last_ur_total_vacancies"] = ur_total
        meta["updated_at"] = checked_at

        events.extend(new_events)
        events = events[-5000:]

        self._write_json(self.listings_file, listings)
        self._write_json(self.events_file, events)
        self._write_json(self.meta_file, meta)
        self._write_json(self.alerts_file, alerts)

        return self._summary(listings, events, new_events, alert_records, meta, checked_at)

    def _new_state(self, item: dict[str, Any], checked_at: str, snapshot_id: str) -> dict[str, Any]:
        state = self._base_state(item)
        state.update(
            {
                "first_seen_at": checked_at,
                "last_seen_at": checked_at,
                "current_appearance_started_at": checked_at,
                "disappeared_at": None,
                "lifetime_minutes": 0.0,
                "appearance_count": 1,
                "consecutive_seen_count": 1,
                "total_seen_checks": 1,
                "current_status": "new",
                "is_present": True,
                "last_snapshot_id": snapshot_id,
                "appearances": [{"start": checked_at, "end": None, "status": "active"}],
            }
        )
        return state

    def _still_seen(
        self,
        previous: dict[str, Any],
        item: dict[str, Any],
        checked_at: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        state = dict(previous)
        state.update(self._base_state(item))
        state["last_seen_at"] = checked_at
        state["consecutive_seen_count"] = int(previous.get("consecutive_seen_count", 0)) + 1
        state["total_seen_checks"] = int(previous.get("total_seen_checks", 0)) + 1
        state["lifetime_minutes"] = minutes_between(
            state.get("current_appearance_started_at"), checked_at
        )
        state["current_status"] = (
            "stable"
            if state["consecutive_seen_count"] >= int(self.rules.get("stable_seen_checks", 3))
            else state.get("current_status", "new")
        )
        state["is_present"] = True
        state["last_snapshot_id"] = snapshot_id
        return state

    def _reappeared(
        self,
        previous: dict[str, Any],
        item: dict[str, Any],
        checked_at: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        state = dict(previous)
        state.update(self._base_state(item))
        state["last_seen_at"] = checked_at
        state["current_appearance_started_at"] = checked_at
        state["disappeared_at"] = None
        state["lifetime_minutes"] = 0.0
        state["appearance_count"] = int(previous.get("appearance_count", 1)) + 1
        state["consecutive_seen_count"] = 1
        state["total_seen_checks"] = int(previous.get("total_seen_checks", 0)) + 1
        state["current_status"] = "reappeared"
        state["is_present"] = True
        state["last_snapshot_id"] = snapshot_id
        appearances = list(previous.get("appearances") or [])
        appearances.append({"start": checked_at, "end": None, "status": "active"})
        state["appearances"] = appearances[-20:]
        return state

    def _disappeared(
        self,
        previous: dict[str, Any],
        checked_at: str,
        snapshot_id: str,
    ) -> tuple[dict[str, Any], str]:
        state = dict(previous)
        lifetime = minutes_between(state.get("current_appearance_started_at"), checked_at)
        fast = lifetime <= float(self.rules.get("fast_disappear_minutes", 15))
        event_type = "disappeared_fast" if fast else "disappeared"
        state["current_status"] = event_type
        state["is_present"] = False
        state["disappeared_at"] = checked_at
        state["lifetime_minutes"] = lifetime
        state["consecutive_seen_count"] = 0
        state["last_snapshot_id"] = snapshot_id
        appearances = list(state.get("appearances") or [])
        if appearances and appearances[-1].get("end") is None:
            appearances[-1]["end"] = checked_at
            appearances[-1]["status"] = event_type
            appearances[-1]["lifetime_minutes"] = lifetime
        state["appearances"] = appearances[-20:]
        return state, event_type

    def _base_state(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "stable_id": item["stable_id"],
            "source": item.get("source", ""),
            "official_id": item.get("official_id", ""),
            "building_name": item.get("building_name", ""),
            "room_no": item.get("room_no", ""),
            "layout": item.get("layout", ""),
            "area": item.get("area", ""),
            "rent": item.get("rent", ""),
            "common_fee": item.get("common_fee", ""),
            "detail_url": item.get("detail_url", ""),
            "is_high_priority": bool(item.get("is_high_priority")),
            "source_label": item.get("source_label", ""),
            "action_hint": item.get("action_hint", ""),
            "raw": item.get("raw", {}),
        }

    def _event(
        self,
        event_type: str,
        state: dict[str, Any],
        checked_at: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        return {
            "event_id": hashlib.sha1(
                f"{state['stable_id']}|{event_type}|{checked_at}|{snapshot_id}".encode("utf-8")
            ).hexdigest()[:20],
            "event_type": event_type,
            "stable_id": state["stable_id"],
            "source": state.get("source"),
            "building_name": state.get("building_name"),
            "room_no": state.get("room_no"),
            "layout": state.get("layout"),
            "area": state.get("area"),
            "rent": state.get("rent"),
            "common_fee": state.get("common_fee"),
            "current_status": state.get("current_status"),
            "first_seen_at": state.get("first_seen_at"),
            "last_seen_at": state.get("last_seen_at"),
            "lifetime_minutes": state.get("lifetime_minutes", 0),
            "appearance_count": state.get("appearance_count", 0),
            "is_high_priority": state.get("is_high_priority", False),
            "created_at": checked_at,
            "snapshot_id": snapshot_id,
        }

    def _summary(
        self,
        listings: dict[str, dict[str, Any]],
        events: list[dict[str, Any]],
        new_events: list[dict[str, Any]],
        alert_records: list[dict[str, Any]],
        meta: dict[str, Any],
        checked_at: str,
    ) -> dict[str, Any]:
        records = list(listings.values())
        active_records = [record for record in records if record.get("is_present")]
        today_start = datetime.fromisoformat(checked_at) - timedelta(hours=24)
        recent_records = [
            record
            for record in records
            if (parse_time(record.get("last_seen_at")) or datetime.min) >= today_start
        ]
        today_priority = sorted(
            recent_records,
            key=lambda item: (
                0 if item.get("building_name") == "ヌーヴェル赤羽台" else 1,
                0 if item.get("is_high_priority") else 1,
                0 if item.get("is_present") else 1,
                -(parse_time(item.get("last_seen_at")) or datetime(1970, 1, 1)).timestamp(),
            ),
        )
        latest_events = [
            event
            for event in sorted(events[-300:], key=lambda item: item.get("created_at", ""), reverse=True)
            if event.get("event_type") != "still_seen"
        ]
        boost_until = meta.get("boost_until")
        boost_active = bool(parse_time(boost_until) and parse_time(boost_until) > datetime.fromisoformat(checked_at))
        return {
            "checked_at": checked_at,
            "records": sorted(active_records, key=lambda item: item.get("last_seen_at", ""), reverse=True),
            "today_priority": today_priority[:50],
            "latest_events": latest_events[:80],
            "new_events": new_events,
            "alerts": alert_records,
            "boost": {
                "active": boost_active,
                "until": boost_until,
                "reason": meta.get("boost_reason", ""),
                "scopes": meta.get("boost_scopes", []),
                "interval_seconds": int(self.rules.get("event_boost_interval_seconds", 60)),
            },
            "data_files": {
                "listings": str(self.listings_file),
                "events": str(self.events_file),
                "snapshots_dir": str(self.snapshots_dir),
                "evidence_dir": str(self.evidence_dir),
                "alerts": str(self.alerts_file),
            },
        }

    def _quick_tags(self, item: dict[str, Any], state: dict[str, Any] | None) -> list[str]:
        tags: list[str] = []
        rules = self.rules.get("quick_judgement", {})
        rent = parse_money(str(item.get("rent") or ""))
        area = parse_area(str(item.get("area") or ""))
        building = str(item.get("building_name") or "")
        status = (state or {}).get("current_status") or item.get("current_status")
        if item.get("source") == "UR":
            tags.append("参考向")
        if item.get("source") == "JKK":
            tags.append("建议手动登录确认")
        if item.get("is_high_priority"):
            tags.append("高优先级")
        if "赤羽台" in building:
            tags.append("赤羽台")
        if rent is not None and rent <= int(rules.get("low_rent_yen", 100000)):
            tags.append("低价")
        if rent is not None and rent >= int(rules.get("high_rent_yen", 200000)):
            tags.append("高价")
        if area is not None and area <= float(rules.get("small_area_m2", 40)):
            tags.append("小户型")
        if area is not None and area >= float(rules.get("large_area_m2", 60)):
            tags.append("大户型")
        if status == "disappeared_fast":
            tags.append("快速消失")
        if state and int(state.get("appearance_count", 0)) > 1:
            tags.append("重复出现")
        return tags

    def _is_high_priority(self, item: dict[str, Any]) -> bool:
        source = item.get("source")
        name = str(item.get("building_name") or "")
        key = "high_priority_ur_names" if source == "UR" else "high_priority_jkk_names"
        return any(target in name for target in self.rules.get(key, []))

    def _boost_scope(self, state: dict[str, Any]) -> str:
        source = state.get("source") or "-"
        name = state.get("building_name") or "-"
        return f"{source}:{name}"

    def _should_alert(self, state: dict[str, Any], alerts: dict[str, Any], checked_at: str) -> bool:
        if not state.get("is_high_priority") or not state.get("is_present"):
            return False
        stable_id = state["stable_id"]
        previous = alerts.get(stable_id) or {}
        if previous.get("last_status") != state.get("current_status"):
            return True
        last_alert = parse_time(previous.get("last_alert_at"))
        if not last_alert:
            return True
        cooldown = timedelta(minutes=int(self.rules.get("popup_cooldown_minutes", 30)))
        return datetime.fromisoformat(checked_at) - last_alert >= cooldown

    def _save_snapshot(
        self,
        checked_at: str,
        current_records: list[dict[str, Any]],
        snapshot_meta: dict[str, Any],
    ) -> str:
        stamp = checked_at.replace(":", "").replace("-", "").replace("T", "_")
        snapshot_id = f"snapshot_{stamp}"
        path = self.snapshots_dir / f"{snapshot_id}.json"
        self._write_json(
            path,
            {
                "snapshot_id": snapshot_id,
                "checked_at": checked_at,
                "meta": snapshot_meta,
                "records": current_records,
            },
        )
        return snapshot_id

    def _save_evidence(
        self,
        state: dict[str, Any],
        checked_at: str,
        snapshot_id: str,
        reason: str,
    ) -> None:
        stamp = checked_at.replace(":", "").replace("-", "").replace("T", "_")
        path = self.evidence_dir / f"{state['stable_id']}_{stamp}.json"
        self._write_json(
            path,
            {
                "reason": reason,
                "snapshot_id": snapshot_id,
                "captured_at": checked_at,
                "listing": state,
            },
        )

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
