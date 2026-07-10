# -*- coding: utf-8 -*-
"""
Small local watcher for JKK Tokyo's "先着順あき家検索" page.

The site is old and session-heavy, so this tool starts from the public entry
point every time, submits the official search form, then parses the result
table. It uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable

from watch_lifecycle import ListingLifecycleStore, load_watch_rules, make_stable_id


try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE = "https://jhomes.to-kousya.or.jp/search/jkknet/service/"
INIT_URL = BASE + "akiyaJyoukenStartInit"
SEARCH_ACTION = BASE + "akiyaJyoukenRef"
SEARCH_TOKEN = "E17511BF89D3A101AFEF10EBF1587561"
DETAIL_TOKEN = "26131A06F36B4487BA38B2958068CA6B"
DEFAULT_TARGETS = ["コーシャハイム加賀", "コーシャハイム田端テラス"]
DEFAULT_EXCLUDES = ["カーメスト用賀馬事公苑"]
UR_RESULT_URL = "https://www.ur-net.go.jp/chintai/kanto/tokyo/result/?skcs=117&skcs=117&rent_low=&rent_high=&rent_low=&rent_high=&walk=&walk=&floorspace_low=&floorspace_high=&floorspace_low=&floorspace_high=&years=&years=&tdfk=13&todofuken=tokyo"
UR_API_BASE = "https://chintai.r6.ur-net.go.jp/chintai/api/"
DEFAULT_UR_TARGETS = ["ヌーヴェル赤羽台"]
DEFAULT_INTERVAL_SECONDS = 10 * 60
DEFAULT_FAST_INTERVAL_SECONDS = 5 * 60
DEFAULT_STATS_BUCKET_MINUTES = 60
DEFAULT_NEWER_THAN_YEAR = 2010
DEFAULT_ALERT_DIR = Path(__file__).resolve().parent
DEFAULT_STATS_FILE = DEFAULT_ALERT_DIR / "watch_stats.json"
DEFAULT_RULES_FILE = DEFAULT_ALERT_DIR / "config" / "watch_rules.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)


@dataclass
class Listing:
    name: str
    area: str
    priority: str
    housing_type: str
    layout: str
    floor_area_m2: str
    rent_yen: str
    common_fee_yen: str
    units: str
    detail_onclick: str = ""
    detail_params: list[str] | None = None
    building_year: int | None = None
    building_year_label: str = ""
    detail_error: str = ""
    is_target: bool = False
    is_excluded: bool = False
    is_newer_building: bool = False
    stable_id: str = ""
    current_status: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    lifetime_minutes: float = 0.0
    appearance_count: int = 0
    quick_tags: list[str] | None = None


@dataclass
class UrRoom:
    building: str
    room_no: str
    layout: str
    floor_area_m2: str
    floor: str
    rent_yen: str
    common_fee_yen: str
    detail_url: str
    stable_id: str = ""
    current_status: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    lifetime_minutes: float = 0.0
    appearance_count: int = 0
    quick_tags: list[str] | None = None


@dataclass
class UrListing:
    name: str
    place: str
    traffic: str
    room_count: int
    detail_url: str
    shisya: str = ""
    danchi: str = ""
    shikibetu: str = ""
    is_target: bool = False
    rooms: list[UrRoom] | None = None


@dataclass
class UrReport:
    checked_at: str
    source_url: str
    target_names: list[str]
    target_found: bool
    total_properties: int
    total_vacancies: int
    listings: list[UrListing]
    warning: str | None = None


@dataclass
class CheckReport:
    checked_at: str
    total_count: int | None
    target_names: list[str]
    exclude_names: list[str]
    target_found: bool
    visible_count: int
    excluded_count: int
    listings: list[Listing]
    newer_than_year: int = DEFAULT_NEWER_THAN_YEAR
    ur_report: UrReport | None = None
    lifecycle_summary: dict | None = None
    source_url: str = INIT_URL
    warning: str | None = None


class FormParser(HTMLParser):
    """Collect successful-control defaults from the JKK search form."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[tuple[str, str]] = []
        self.ku_values: list[str] = []
        self._select: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag == "input":
            input_type = attrs_dict.get("type", "text").lower()
            name = attrs_dict.get("name")
            value = attrs_dict.get("value", "")
            disabled = "disabled" in attrs_dict

            if attrs_dict.get("id") == "ku" and not disabled and value:
                self.ku_values.append(value)

            if not name or disabled:
                return
            if input_type in {"button", "submit", "image", "reset"}:
                return
            if input_type in {"checkbox", "radio"} and "checked" not in attrs_dict:
                return
            self.fields.append((name, value))
            return

        if tag == "select":
            self._select = {
                "name": attrs_dict.get("name"),
                "disabled": "disabled" in attrs_dict,
                "options": [],
            }
            return

        if tag == "option" and self._select is not None:
            self._select["options"].append(
                (attrs_dict.get("value", ""), "selected" in attrs_dict)
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "select" or self._select is None:
            return
        name = self._select["name"]
        options = self._select["options"]
        if name and not self._select["disabled"] and options:
            value = next((value for value, selected in options if selected), options[0][0])
            self.fields.append((name, value))
        self._select = None


class ResultParser(HTMLParser):
    """Extract table rows from the legacy result HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._onclicks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag == "tr":
            self._row = []
            self._onclicks = []
            return

        if self._row is not None and tag in {"td", "th"}:
            self._cell = []

        if self._row is not None and "onclick" in attrs_dict:
            self._onclicks.append(attrs_dict["onclick"])

        if self._cell is not None:
            if "alt" in attrs_dict:
                self._cell.append(attrs_dict["alt"])
            if tag in {"input", "button"} and attrs_dict.get("value"):
                self._cell.append(attrs_dict["value"])

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)
            self._cell = None
            return

        if tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append((self._row, self._onclicks[:]))
            self._row = None
            self._onclicks = []


class JkkClient:
    def __init__(self, timeout: int = 20, verify_ssl: bool = False) -> None:
        self.timeout = timeout
        self.context = ssl.create_default_context()
        if not verify_ssl:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.context),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def _open(self, request: urllib.request.Request | str) -> bytes:
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read()

    def _start_form(self) -> tuple[list[tuple[str, str]], list[str]]:
        self._open(INIT_URL)
        data = urllib.parse.urlencode({"redirect": "true", "url": INIT_URL}).encode("ascii")
        request = urllib.request.Request(INIT_URL, data=data, method="POST")
        form_html = self._open(request).decode("cp932", errors="ignore")
        parser = FormParser()
        parser.feed(form_html)
        if not parser.ku_values:
            raise RuntimeError("JKK search form was loaded, but ward checkboxes were not found.")
        return parser.fields, parser.ku_values

    def search_wards(
        self,
        targets: Iterable[str] = DEFAULT_TARGETS,
        excludes: Iterable[str] = DEFAULT_EXCLUDES,
        name_kana: str = "",
        read_detail_years: bool = True,
        newer_than_year: int = DEFAULT_NEWER_THAN_YEAR,
    ) -> CheckReport:
        fields, ku_values = self._start_form()
        payload = self._build_search_payload(fields, ku_values, name_kana=name_kana)
        body = urllib.parse.urlencode(payload, encoding="cp932").encode("ascii")
        request = urllib.request.Request(
            SEARCH_ACTION,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result_html = self._open(request).decode("cp932", errors="ignore")
        result_pages = self._expand_result_pages(result_html, list(targets), list(excludes))
        report = parse_result_pages(result_pages, list(targets), list(excludes))
        report.newer_than_year = newer_than_year
        if read_detail_years and result_pages:
            self._enrich_detail_years(report, result_pages[0], newer_than_year)
        return report

    def _expand_result_pages(
        self,
        result_html: str,
        targets: list[str],
        excludes: list[str],
        max_pages: int = 20,
    ) -> list[str]:
        pages = [result_html]
        report = parse_result_page(result_html, targets, excludes)

        if report.total_count and report.total_count > len(report.listings):
            expanded = self._post_result_form(
                result_html,
                "AKIYAchangeCount",
                {"akiyaRefRM.showCount": "50"},
            )
            expanded_report = parse_result_page(expanded, targets, excludes)
            if len(expanded_report.listings) >= len(report.listings):
                pages = [expanded]
                report = expanded_report

        seen_html = {pages[0]}
        while report.total_count and len(parse_result_pages(pages, targets, excludes).listings) < report.total_count:
            if len(pages) >= max_pages:
                break
            action = self._paging_action(pages[-1], "afterPage")
            next_html = self._post_result_form(pages[-1], action)
            if next_html in seen_html:
                break
            seen_html.add(next_html)
            before_count = len(parse_result_pages(pages, targets, excludes).listings)
            pages.append(next_html)
            after_report = parse_result_pages(pages, targets, excludes)
            if len(after_report.listings) <= before_count:
                break
        return pages

    def _post_result_form(
        self,
        result_html: str,
        action_name: str,
        overrides: dict[str, str] | None = None,
    ) -> str:
        parser = FormParser()
        parser.feed(result_html)
        overrides = overrides or {}
        payload: list[tuple[str, str]] = []
        used = set()
        for key, value in parser.fields:
            if key in overrides:
                value = overrides[key]
                used.add(key)
            payload.append((key, value))
        for key, value in overrides.items():
            if key not in used:
                payload.append((key, value))

        body = urllib.parse.urlencode(payload, encoding="cp932").encode("ascii")
        request = urllib.request.Request(
            BASE + action_name,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._open(request).decode("cp932", errors="ignore")

    def _detail_page(self, result_html: str, listing: Listing) -> str:
        if not listing.detail_params or len(listing.detail_params) != 4:
            raise RuntimeError("Detail parameters were not found for this listing.")
        boshu_no, msk_kbn, jyutaku_cd, yusen_kbn = listing.detail_params
        return self._post_result_form(
            result_html,
            "akiyaSenDet",
            {
                "jklm": self._detail_token(result_html),
                "akiyaRefRM.akiyaDatM.boshuNo": boshu_no,
                "akiyaRefRM.akiyaDatM.mskKbn": msk_kbn,
                "akiyaRefRM.akiyaDatM.jyutakuCd": jyutaku_cd,
                "akiyaRefRM.akiyaDatM.yusenKbn": yusen_kbn,
            },
        )

    def _enrich_detail_years(
        self,
        report: CheckReport,
        result_html: str,
        newer_than_year: int = DEFAULT_NEWER_THAN_YEAR,
    ) -> None:
        for listing in report.listings:
            if listing.is_excluded and not listing.is_target:
                continue
            if not listing.detail_params:
                continue
            try:
                detail_html = self._detail_page(result_html, listing)
                building_year, label = parse_building_year(detail_html)
                listing.building_year = building_year
                listing.building_year_label = label
                listing.is_newer_building = (
                    building_year is not None and building_year >= newer_than_year
                )
            except Exception as error:
                listing.detail_error = str(error)

    @staticmethod
    def _detail_token(result_html: str) -> str:
        match = re.search(r"xyz\.value\s*=\s*['\"]([^'\"]+)['\"]", result_html)
        if match:
            return match.group(1)
        return DETAIL_TOKEN

    @staticmethod
    def _paging_action(result_html: str, method: str) -> str:
        parser = FormParser()
        parser.feed(result_html)
        paging_url = next(
            (value for key, value in parser.fields if key == "pagingInputDataGrid_url"),
            "AKIYA",
        )
        return paging_url + method

    @staticmethod
    def _build_search_payload(
        fields: list[tuple[str, str]], ku_values: list[str], name_kana: str = ""
    ) -> list[tuple[str, str]]:
        removed_names = {
            "akiyaInitRM.akiyaRefM.checks",
            "akiyaInitRM.akiyaRefM.allCheck",
        }
        payload = [(key, value) for key, value in fields if key not in removed_names]
        payload = (
            [("akiyaInitRM.akiyaRefM.allCheck", "ALLKU")]
            + [("akiyaInitRM.akiyaRefM.checks", value) for value in ku_values]
            + payload
        )

        seen_required_time = False
        seen_building_age = False
        seen_jklm = False
        updated: list[tuple[str, str]] = []
        for key, value in payload:
            if key == "jklm":
                value = SEARCH_TOKEN
                seen_jklm = True
            elif key == "akiyaInitRM.akiyaRefM.jyutakuKanaName":
                value = name_kana
            elif key == "akiyaInitRM.akiyaRefM.requiredTime":
                value = "99"
                seen_required_time = True
            elif key == "akiyaInitRM.akiyaRefM.chikuNensu":
                value = "99"
                seen_building_age = True
            updated.append((key, value))

        if not seen_jklm:
            updated.append(("jklm", SEARCH_TOKEN))
        if not seen_required_time:
            updated.append(("akiyaInitRM.akiyaRefM.requiredTime", "99"))
        if not seen_building_age:
            updated.append(("akiyaInitRM.akiyaRefM.chikuNensu", "99"))
        return updated


class UrClient:
    def __init__(self, timeout: int = 20, verify_ssl: bool = True) -> None:
        self.timeout = timeout
        self.context = ssl.create_default_context()
        if not verify_ssl:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.context)
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def search_kita(
        self,
        targets: Iterable[str] = DEFAULT_UR_TARGETS,
        source_url: str = UR_RESULT_URL,
    ) -> UrReport:
        target_list = list(targets)
        base_payload = [
            ("mode", "area"),
            ("skcs", "117"),
            ("block", "kanto"),
            ("tdfk", "13"),
            ("rireki_tdfk", "13"),
            ("orderByField", "0"),
            ("pageSize", "50"),
            ("pageIndex", "0"),
            ("shisya", ""),
            ("danchi", ""),
            ("shikibetu", ""),
            ("pageIndexRoom", "0"),
            ("sp", ""),
        ]
        data = self._post_api("bukken/result/bukken_result/", base_payload, source_url)
        listings: list[UrListing] = []
        for item in data if isinstance(data, list) else []:
            name = str(item.get("danchiNm") or "")
            room_count = parse_int(item.get("roomCount"))
            listing = UrListing(
                name=name,
                place=str(item.get("place") or ""),
                traffic=clean_ur_text(str(item.get("traffic") or "")),
                room_count=room_count,
                detail_url=absolute_ur_url(
                    f"/chintai/kanto/tokyo/{item.get('shisya', '')}_{item.get('danchi', '')}{item.get('shikibetu', '')}.html"
                ),
                shisya=str(item.get("shisya") or ""),
                danchi=str(item.get("danchi") or ""),
                shikibetu=str(item.get("shikibetu") or ""),
                is_target=any(target in name for target in target_list),
                rooms=[],
            )
            if room_count > 0:
                listing.rooms = self._rooms_for_listing(listing, base_payload, source_url)
            listings.append(listing)

        first_row = data[0] if isinstance(data, list) and data else {}
        total_properties = parse_int(first_row.get("bukkenCount")) if first_row else len(listings)
        total_vacancies = parse_int(first_row.get("allCount")) if first_row else 0
        target_found = any(
            listing.is_target and listing.room_count > 0 for listing in listings
        )
        return UrReport(
            checked_at=datetime.now().isoformat(timespec="seconds"),
            source_url=source_url,
            target_names=target_list,
            target_found=target_found,
            total_properties=total_properties,
            total_vacancies=total_vacancies,
            listings=listings,
        )

    def _rooms_for_listing(
        self,
        listing: UrListing,
        base_payload: list[tuple[str, str]],
        source_url: str,
    ) -> list[UrRoom]:
        payload = [
            (key, value)
            for key, value in base_payload
            if key not in {"shisya", "danchi", "shikibetu", "pageIndexRoom"}
        ]
        payload.extend(
            [
                ("shisya", listing.shisya),
                ("danchi", listing.danchi),
                ("shikibetu", listing.shikibetu),
                ("pageIndexRoom", "0"),
            ]
        )
        data = self._post_api("bukken/result/bukken_result_room/", payload, source_url)
        rooms: list[UrRoom] = []
        for item in data if isinstance(data, list) else []:
            rooms.append(
                UrRoom(
                    building=clean_ur_text(str(item.get("roomNmMain") or "")),
                    room_no=clean_ur_text(str(item.get("roomNmSub") or "")),
                    layout=clean_ur_text(str(item.get("type") or "")),
                    floor_area_m2=clean_ur_text(str(item.get("floorspace") or "")),
                    floor=clean_ur_text(str(item.get("floor") or "")),
                    rent_yen=clean_ur_text(str(item.get("rent") or "")),
                    common_fee_yen=clean_ur_text(str(item.get("commonfee") or "")),
                    detail_url=absolute_ur_url(str(item.get("roomLinkPc") or "")),
                )
            )
        return rooms

    def _post_api(
        self,
        endpoint: str,
        payload: list[tuple[str, str]],
        referer: str,
    ) -> object:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            UR_API_BASE + endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://www.ur-net.go.jp",
                "Referer": referer,
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def parse_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def clean_ur_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = value.replace("\u337f", "m2").replace("㎡", "m2")
    return " ".join(value.split())


def absolute_ur_url(path: str) -> str:
    if not path:
        return ""
    return urllib.parse.urljoin("https://www.ur-net.go.jp", path)


class FetchLock:
    def __init__(self, base_dir: str | Path, stale_seconds: int = 300) -> None:
        self.lock_path = Path(base_dir) / "data" / "fetch.lock"
        self.stale_seconds = stale_seconds
        self.fd: int | None = None

    def __enter__(self) -> "FetchLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_stale_lock()
        try:
            self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"{os.getpid()} {time.time()}".encode("ascii"))
        except FileExistsError as error:
            raise RuntimeError("Another check is already running; skipped this cycle.") from error
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _clear_stale_lock(self) -> None:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        if age > self.stale_seconds:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def parse_result_page(
    result_html: str, targets: list[str], excludes: list[str]
) -> CheckReport:
    parser = ResultParser()
    parser.feed(result_html)

    total_count = None
    count_match = re.search(r"(\d+)\s*件が該当", result_html)
    if count_match:
        total_count = int(count_match.group(1))

    listings: list[Listing] = []
    for cells, onclicks in parser.rows:
        if len(cells) < 11:
            continue
        if cells[1] in {"住宅名", ""}:
            continue
        if cells[-1] != "詳細":
            continue

        detail_onclick = next((value for value in onclicks if "senPage(" in value), "")
        detail_params = parse_detail_params(detail_onclick)
        name = cells[1]
        listing = Listing(
            name=name,
            area=cells[2],
            priority=cells[3],
            housing_type=cells[4],
            layout=cells[5],
            floor_area_m2=cells[6],
            rent_yen=cells[7],
            common_fee_yen=cells[8],
            units=cells[9],
            detail_onclick=detail_onclick,
            detail_params=detail_params,
            is_target=any(target in name for target in targets),
            is_excluded=any(exclude in name for exclude in excludes),
        )
        listings.append(listing)

    visible_count = sum(1 for listing in listings if not listing.is_excluded)
    excluded_count = sum(1 for listing in listings if listing.is_excluded)
    target_found = any(listing.is_target for listing in listings)

    warning = None
    if total_count is not None and total_count != len(listings):
        warning = f"Result count was {total_count}, but parsed {len(listings)} table rows."

    return CheckReport(
        checked_at=datetime.now().isoformat(timespec="seconds"),
        total_count=total_count,
        target_names=targets,
        exclude_names=excludes,
        target_found=target_found,
        visible_count=visible_count,
        excluded_count=excluded_count,
        listings=listings,
        warning=warning,
    )


def parse_result_pages(
    result_html_pages: list[str], targets: list[str], excludes: list[str]
) -> CheckReport:
    if not result_html_pages:
        return CheckReport(
            checked_at=datetime.now().isoformat(timespec="seconds"),
            total_count=None,
            target_names=targets,
            exclude_names=excludes,
            target_found=False,
            visible_count=0,
            excluded_count=0,
            listings=[],
            warning="No result pages were parsed.",
        )

    first = parse_result_page(result_html_pages[0], targets, excludes)
    listings: list[Listing] = []
    seen = set()
    for result_html in result_html_pages:
        page_report = parse_result_page(result_html, targets, excludes)
        for listing in page_report.listings:
            key = (
                listing.name,
                listing.area,
                listing.layout,
                listing.floor_area_m2,
                listing.rent_yen,
                listing.common_fee_yen,
                listing.units,
                listing.detail_onclick,
            )
            if key in seen:
                continue
            seen.add(key)
            listings.append(listing)

    visible_count = sum(1 for listing in listings if not listing.is_excluded)
    excluded_count = sum(1 for listing in listings if listing.is_excluded)
    target_found = any(listing.is_target for listing in listings)
    warning = None
    if first.total_count is not None and first.total_count != len(listings):
        warning = f"Result count was {first.total_count}, but parsed {len(listings)} table rows across pages."

    return CheckReport(
        checked_at=first.checked_at,
        total_count=first.total_count,
        target_names=targets,
        exclude_names=excludes,
        target_found=target_found,
        visible_count=visible_count,
        excluded_count=excluded_count,
        listings=listings,
        warning=warning,
    )


def parse_detail_params(onclick: str) -> list[str] | None:
    match = re.search(
        r"senPage\('([^']*)','([^']*)','([^']*)','([^']*)'\)", onclick
    )
    if not match:
        return None
    return list(match.groups())


BUILDING_YEAR_KEYWORDS = (
    "竣工",
    "建設",
    "建築",
    "完成",
    "築年",
    "築年月",
    "建物",
)
ERA_BASE_YEARS = {
    "令和": 2018,
    "平成": 1988,
    "昭和": 1925,
}


def parse_building_year(detail_html: str) -> tuple[int | None, str]:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", detail_html))
    plain = " ".join(plain.split())

    snippets: list[str] = []
    for keyword in BUILDING_YEAR_KEYWORDS:
        for match in re.finditer(re.escape(keyword), plain):
            start = max(0, match.start() - 40)
            end = min(len(plain), match.end() + 120)
            snippets.append(plain[start:end])

    for snippet in snippets:
        found = find_year_in_text(snippet)
        if not found:
            continue
        year, raw_label = found
        label = compact_year_label(snippet, raw_label)
        return year, label
    return None, ""


def find_year_in_text(text: str) -> tuple[int, str] | None:
    date_match = re.search(r"\b((?:19|20)\d{2}[/-]\d{1,2}(?:[/-]\d{1,2})?)\b", text)
    if date_match:
        raw = date_match.group(1)
        return int(raw[:4]), raw

    year_match = re.search(r"\b((?:19|20)\d{2})\s*年", text)
    if year_match:
        raw = year_match.group(0)
        return int(year_match.group(1)), raw

    era_match = re.search(r"(令和|平成|昭和)\s*(元|\d{1,2})\s*年", text)
    if era_match:
        era, era_year_text = era_match.groups()
        era_year = 1 if era_year_text == "元" else int(era_year_text)
        return ERA_BASE_YEARS[era] + era_year, era_match.group(0)

    return None


def compact_year_label(snippet: str, raw_label: str) -> str:
    label_start = 0
    for keyword in BUILDING_YEAR_KEYWORDS:
        keyword_index = snippet.find(keyword)
        if keyword_index >= 0:
            label_start = keyword_index
            break
    raw_index = snippet.find(raw_label)
    if raw_index < 0:
        return raw_label
    label_end = raw_index + len(raw_label)
    return snippet[label_start:label_end].strip(" :：-/")


def alert_listings(report: CheckReport) -> list[Listing]:
    return [
        listing
        for listing in report.listings
        if listing.is_target or listing.is_newer_building
    ]


def ur_alert_listings(report: CheckReport) -> list[UrListing]:
    if report.ur_report is None:
        return []
    return [
        listing
        for listing in report.ur_report.listings
        if listing.is_target and listing.room_count > 0
    ]


def listing_marker(listing: Listing) -> str:
    if listing.is_target:
        return "TARGET"
    if listing.is_newer_building:
        return "2010+"
    if listing.is_excluded:
        return "excluded"
    return "candidate"


def listing_year_text(listing: Listing) -> str:
    if listing.building_year_label:
        return listing.building_year_label
    if listing.building_year is not None:
        return str(listing.building_year)
    if listing.detail_error:
        return "detail error"
    return ""


def ur_room_summary(room: UrRoom) -> str:
    return " / ".join(
        value
        for value in [
            room.building,
            room.room_no,
            room.layout,
            room.floor_area_m2,
            room.floor,
            room.rent_yen,
            f"共益費 {room.common_fee_yen}" if room.common_fee_yen else "",
        ]
        if value
    )


def ur_listing_summary(listing: UrListing) -> str:
    room_text = "; ".join(ur_room_summary(room) for room in (listing.rooms or []))
    return " | ".join(
        value
        for value in [
            listing.name,
            listing.place,
            f"空室 {listing.room_count}",
            room_text,
        ]
        if value
    )


def lifecycle_records_from_report(report: CheckReport, rules: dict) -> list[dict]:
    records: list[dict] = []
    for listing in report.listings:
        if listing.is_excluded and not listing.is_target:
            continue
        detail_key = ":".join(listing.detail_params or [])
        record = {
            "source": "JKK",
            "source_label": "JKK",
            "official_id": f"jkk:{detail_key}" if detail_key else "",
            "building_name": listing.name,
            "room_no": "",
            "layout": listing.layout,
            "area": listing.floor_area_m2,
            "rent": listing.rent_yen,
            "common_fee": listing.common_fee_yen,
            "detail_url": report.source_url,
            "is_high_priority": listing.is_target or listing.is_newer_building,
            "action_hint": "发现新房，请尽快手动登录官网确认。",
            "raw": asdict(listing),
        }
        record["stable_id"] = make_stable_id(record)
        listing.stable_id = record["stable_id"]
        records.append(record)

    if report.ur_report:
        for listing in report.ur_report.listings:
            for room in listing.rooms or []:
                room_no = " ".join(
                    part for part in [room.building, room.room_no] if part
                )
                record = {
                    "source": "UR",
                    "source_label": "UR",
                    "official_id": f"ur:{listing.shisya}:{listing.danchi}:{listing.shikibetu}:{room.detail_url or room_no}",
                    "building_name": listing.name,
                    "room_no": room_no,
                    "layout": room.layout,
                    "area": room.floor_area_m2,
                    "rent": room.rent_yen,
                    "common_fee": room.common_fee_yen,
                    "detail_url": room.detail_url or listing.detail_url,
                    "is_high_priority": listing.is_target
                    or any(target in listing.name for target in rules.get("high_priority_ur_names", [])),
                    "action_hint": "发现参考房源，请关注户型/价格/楼层；如需行动，请电话或线下确认。",
                    "raw": {
                        "listing": asdict(listing),
                        "room": asdict(room),
                    },
                }
                record["stable_id"] = make_stable_id(record)
                room.stable_id = record["stable_id"]
                records.append(record)
    return records


def apply_lifecycle_summary(report: CheckReport, summary: dict | None) -> None:
    if not summary:
        return
    states = {record.get("stable_id"): record for record in summary.get("records", [])}
    states.update(
        {
            record.get("stable_id"): record
            for record in summary.get("today_priority", [])
            if record.get("is_present")
        }
    )
    for listing in report.listings:
        apply_lifecycle_state_to_object(listing, states.get(listing.stable_id))
    if report.ur_report:
        for listing in report.ur_report.listings:
            for room in listing.rooms or []:
                apply_lifecycle_state_to_object(room, states.get(room.stable_id))


def apply_lifecycle_state_to_object(target: object, state: dict | None) -> None:
    if not state:
        return
    for attr, key in [
        ("current_status", "current_status"),
        ("first_seen_at", "first_seen_at"),
        ("last_seen_at", "last_seen_at"),
        ("lifetime_minutes", "lifetime_minutes"),
        ("appearance_count", "appearance_count"),
        ("quick_tags", "quick_tags"),
    ]:
        if hasattr(target, attr):
            setattr(target, attr, state.get(key))


def lifecycle_alert_records(report: CheckReport) -> list[dict]:
    if not report.lifecycle_summary:
        return []
    return list(report.lifecycle_summary.get("alerts") or [])


def report_to_dict(report: CheckReport, include_excluded: bool = True) -> dict:
    data = asdict(report)
    if not include_excluded:
        data["listings"] = [
            item for item in data["listings"] if not item.get("is_excluded")
        ]
    return data


def format_report(report: CheckReport, include_excluded: bool = False) -> str:
    newer_count = sum(1 for listing in report.listings if listing.is_newer_building)
    lines = [
        f"Checked: {report.checked_at}",
        f"JKK total: {report.total_count if report.total_count is not None else 'unknown'}",
        f"Targets: {', '.join(report.target_names)}",
    ]
    if report.target_found:
        lines.append("TARGET FOUND")
    elif newer_count:
        lines.append(f"No target yet, but {newer_count} listing(s) built in/after {report.newer_than_year} appeared.")
    elif report.visible_count:
        lines.append(f"No target yet, but {report.visible_count} non-excluded listing(s) appeared.")
    else:
        lines.append("No target yet. Only excluded listings were found." if report.excluded_count else "No listings found.")

    if report.excluded_count:
        lines.append(f"Excluded: {report.excluded_count} listing(s) matching {', '.join(report.exclude_names)}")
    if report.warning:
        lines.append(f"Warning: {report.warning}")

    shown = [
        listing
        for listing in report.listings
        if include_excluded or not listing.is_excluded or listing.is_target or listing.is_newer_building
    ]
    if shown:
        lines.append("")
        for listing in shown:
            marker = listing_marker(listing)
            year_text = listing_year_text(listing)
            year_suffix = f" | built {year_text}" if year_text else ""
            lines.append(
                f"[{marker}] {listing.name} | {listing.area} | {listing.layout} | "
                f"{listing.floor_area_m2} m2 | {listing.rent_yen} yen | units {listing.units}{year_suffix}"
            )
    if report.ur_report:
        ur = report.ur_report
        lines.append("")
        lines.append(
            f"UR Kita total: {ur.total_properties} buildings / {ur.total_vacancies} vacant room(s)"
        )
        lines.append(f"UR targets: {', '.join(ur.target_names)}")
        if ur.target_found:
            lines.append("UR TARGET FOUND")
        if ur.warning:
            lines.append(f"UR warning: {ur.warning}")
        for listing in ur.listings:
            marker = (
                "UR TARGET"
                if listing.is_target and listing.room_count > 0
                else ("UR target/no vacancy" if listing.is_target else "UR")
            )
            lines.append(f"[{marker}] {ur_listing_summary(listing)}")
    return "\n".join(lines)


def bucket_key(dt: datetime, bucket_minutes: int = DEFAULT_STATS_BUCKET_MINUTES) -> str:
    minute = (dt.minute // bucket_minutes) * bucket_minutes
    return f"{dt.hour:02d}:{minute:02d}"


def report_datetime(report: CheckReport) -> datetime:
    try:
        return datetime.fromisoformat(report.checked_at)
    except ValueError:
        return datetime.now()


def candidate_stats_key(listing: Listing) -> str:
    return "|".join(
        [
            listing.name,
            listing.area,
            listing.layout,
            listing.floor_area_m2,
            listing.rent_yen,
            listing.common_fee_yen,
        ]
    )


def candidate_record(listing: Listing, checked_at: str) -> dict:
    return {
        "name": listing.name,
        "area": listing.area,
        "layout": listing.layout,
        "floor_area_m2": listing.floor_area_m2,
        "rent_yen": listing.rent_yen,
        "common_fee_yen": listing.common_fee_yen,
        "units": listing.units,
        "building_year": listing.building_year,
        "building_year_label": listing.building_year_label,
        "first_seen_at": checked_at,
        "last_seen_at": checked_at,
        "seen_count": 0,
    }


def ensure_candidate_seen(bucket: dict) -> dict:
    seen = bucket.setdefault("candidate_seen", {})
    if not isinstance(seen, dict):
        seen = {}
        bucket["candidate_seen"] = seen
    legacy_names = bucket.get("last_candidate_names") or []
    for name in legacy_names:
        if not name:
            continue
        if any(record.get("name") == name and record.get("layout") for record in seen.values()):
            continue
        key = f"{name}|||||"
        seen.setdefault(
            key,
            {
                "name": name,
                "area": "",
                "layout": "",
                "floor_area_m2": "",
                "rent_yen": "",
                "common_fee_yen": "",
                "units": "",
                "building_year": None,
                "building_year_label": "",
                "first_seen_at": bucket.get("last_candidate_at"),
                "last_seen_at": bucket.get("last_candidate_at"),
                "seen_count": int(bucket.get("candidate_checks", 0) or 0),
            },
        )
    bucket["candidate_names_seen"] = sorted(
        {record.get("name", "") for record in seen.values() if record.get("name")}
    )
    return seen


def empty_stats(bucket_minutes: int = DEFAULT_STATS_BUCKET_MINUTES) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "version": 1,
        "bucket_minutes": bucket_minutes,
        "created_at": now,
        "updated_at": now,
        "total_checks": 0,
        "buckets": {},
    }


def load_stats(stats_file: str | Path, bucket_minutes: int = DEFAULT_STATS_BUCKET_MINUTES) -> dict:
    path = Path(stats_file)
    if not path.exists():
        return empty_stats(bucket_minutes)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty_stats(bucket_minutes)
    if not isinstance(data, dict):
        return empty_stats(bucket_minutes)
    data.setdefault("version", 1)
    data.setdefault("bucket_minutes", bucket_minutes)
    data.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    data.setdefault("updated_at", data["created_at"])
    data.setdefault("total_checks", 0)
    data.setdefault("buckets", {})
    return data


def save_stats(stats: dict, stats_file: str | Path) -> None:
    path = Path(stats_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_stats(
    report: CheckReport,
    stats_file: str | Path,
    bucket_minutes: int = DEFAULT_STATS_BUCKET_MINUTES,
) -> dict:
    stats = load_stats(stats_file, bucket_minutes)
    checked_dt = report_datetime(report)
    key = bucket_key(checked_dt, int(stats.get("bucket_minutes") or bucket_minutes))
    buckets = stats.setdefault("buckets", {})
    bucket = buckets.setdefault(
        key,
        {
            "checks": 0,
            "candidate_checks": 0,
            "target_checks": 0,
            "listing_rows": 0,
            "candidate_rows": 0,
            "target_rows": 0,
            "last_checked_at": None,
            "last_candidate_at": None,
            "last_target_at": None,
            "last_candidate_names": [],
            "candidate_names_seen": [],
            "candidate_seen": {},
        },
    )
    candidate_seen = ensure_candidate_seen(bucket)

    candidate_listings = [listing for listing in report.listings if not listing.is_excluded]
    target_listings = [listing for listing in report.listings if listing.is_target]
    bucket["checks"] = int(bucket.get("checks", 0)) + 1
    bucket["listing_rows"] = int(bucket.get("listing_rows", 0)) + len(report.listings)
    bucket["candidate_rows"] = int(bucket.get("candidate_rows", 0)) + len(candidate_listings)
    bucket["target_rows"] = int(bucket.get("target_rows", 0)) + len(target_listings)
    bucket["last_checked_at"] = report.checked_at
    if candidate_listings:
        bucket["candidate_checks"] = int(bucket.get("candidate_checks", 0)) + 1
        bucket["last_candidate_at"] = report.checked_at
        bucket["last_candidate_names"] = sorted({listing.name for listing in candidate_listings})
        for listing in candidate_listings:
            key = candidate_stats_key(listing)
            record = candidate_seen.setdefault(key, candidate_record(listing, report.checked_at))
            record["last_seen_at"] = report.checked_at
            record["units"] = listing.units
            if listing.building_year is not None:
                record["building_year"] = listing.building_year
            if listing.building_year_label:
                record["building_year_label"] = listing.building_year_label
            if listing.building_year is not None or listing.building_year_label:
                for other in candidate_seen.values():
                    if other.get("name") != listing.name:
                        continue
                    if listing.building_year is not None and not other.get("building_year"):
                        other["building_year"] = listing.building_year
                    if listing.building_year_label and not other.get("building_year_label"):
                        other["building_year_label"] = listing.building_year_label
            record["seen_count"] = int(record.get("seen_count", 0)) + 1
        bucket["candidate_names_seen"] = sorted(
            {record.get("name", "") for record in candidate_seen.values() if record.get("name")}
        )
    if target_listings:
        bucket["target_checks"] = int(bucket.get("target_checks", 0)) + 1
        bucket["last_target_at"] = report.checked_at

    stats["updated_at"] = datetime.now().isoformat(timespec="seconds")
    stats["total_checks"] = int(stats.get("total_checks", 0)) + 1
    save_stats(stats, stats_file)
    return stats


def is_hot_bucket(
    stats: dict,
    when: datetime | None = None,
    bucket_minutes: int = DEFAULT_STATS_BUCKET_MINUTES,
) -> bool:
    when = when or datetime.now()
    key = bucket_key(when, int(stats.get("bucket_minutes") or bucket_minutes))
    bucket = stats.get("buckets", {}).get(key, {})
    return int(bucket.get("candidate_checks", 0)) > 0


def summarize_stats(stats: dict, limit: int = 12) -> dict:
    rows = []
    for key, bucket in stats.get("buckets", {}).items():
        candidate_seen = ensure_candidate_seen(bucket)
        checks = int(bucket.get("checks", 0))
        candidate_checks = int(bucket.get("candidate_checks", 0))
        target_checks = int(bucket.get("target_checks", 0))
        candidate_rate = (candidate_checks / checks) if checks else 0.0
        candidate_seen_rows = sorted(
            candidate_seen.values(),
            key=lambda record: (
                record.get("name", ""),
                record.get("layout", ""),
                record.get("rent_yen", ""),
            ),
        )
        rows.append(
            {
                "bucket": key,
                "checks": checks,
                "candidate_checks": candidate_checks,
                "target_checks": target_checks,
                "candidate_rate": round(candidate_rate, 4),
                "hot": candidate_checks > 0,
                "last_checked_at": bucket.get("last_checked_at"),
                "last_candidate_at": bucket.get("last_candidate_at"),
                "last_candidate_names": bucket.get("last_candidate_names", []),
                "candidate_names_seen": bucket.get("candidate_names_seen", []),
                "candidate_seen": candidate_seen_rows,
            }
        )
    rows.sort(key=lambda item: (-int(item["candidate_checks"]), -float(item["candidate_rate"]), item["bucket"]))
    return {
        "updated_at": stats.get("updated_at"),
        "bucket_minutes": stats.get("bucket_minutes", DEFAULT_STATS_BUCKET_MINUTES),
        "total_checks": stats.get("total_checks", 0),
        "hot_buckets": [row for row in rows if row["hot"]],
        "top_buckets": rows[:limit],
    }


def format_stats(stats: dict, limit: int = 12) -> str:
    summary = summarize_stats(stats, limit=limit)
    lines = [
        f"统计更新时间: {summary.get('updated_at') or '暂无'}",
        f"统计粒度: {summary.get('bucket_minutes')} 分钟",
        f"总检查次数: {summary.get('total_checks')}",
        "",
    ]
    hot = summary["hot_buckets"]
    if not hot:
        lines.append("还没有出现过非排除房源，自适应刷新会继续保持普通间隔。")
        return "\n".join(lines)

    lines.append("5 分钟刷新时间段:")
    for row in hot[:limit]:
        rate = row["candidate_rate"] * 100
        lines.append(
            f"- {row['bucket']} | 出现非排除房源 {row['candidate_checks']}/{row['checks']} 次检查 | "
            f"出现率 {rate:.1f}% | 最近出现 {row.get('last_candidate_at') or '-'}"
        )
        for record in row.get("candidate_seen") or []:
            detail = " / ".join(
                value
                for value in [
                    record.get("name", ""),
                    record.get("area", ""),
                    record.get("layout", ""),
                    record.get("rent_yen", ""),
                    record.get("building_year_label", "") or str(record.get("building_year") or ""),
                    f"累计出现 {record.get('seen_count', 0)} 次",
                ]
                if value
            )
            lines.append(f"  - {detail}")
    return "\n".join(lines)


def render_html_report(report: CheckReport) -> str:
    rows = []
    for listing in report.listings:
        classes = []
        if listing.is_target:
            classes.append("target")
        if listing.is_newer_building:
            classes.append("newer")
        if listing.is_excluded:
            classes.append("excluded")
        rows.append(
            "<tr class=\"{}\">{}</tr>".format(
                " ".join(classes),
                "".join(
                    f"<td>{html.escape(value)}</td>"
                    for value in [
                        listing_marker(listing),
                        listing.name,
                        listing.area,
                        listing.layout,
                        listing.floor_area_m2,
                        listing.rent_yen,
                        listing.common_fee_yen,
                        listing.units,
                        listing_year_text(listing),
                    ]
                ),
            )
        )
    table = "\n".join(rows) or '<tr><td colspan="9">No listings found.</td></tr>'
    ur_rows = []
    if report.ur_report:
        for listing in report.ur_report.listings:
            classes = ["target"] if listing.is_target and listing.room_count > 0 else []
            rooms = "; ".join(ur_room_summary(room) for room in (listing.rooms or []))
            ur_rows.append(
                "<tr class=\"{}\">{}</tr>".format(
                    " ".join(classes),
                    "".join(
                        f"<td>{html.escape(value)}</td>"
                        for value in [
                            "UR TARGET" if listing.is_target and listing.room_count > 0 else "UR",
                            listing.name,
                            listing.place,
                            str(listing.room_count),
                            rooms,
                        ]
                    ),
                )
            )
    ur_table = "\n".join(ur_rows) or '<tr><td colspan="5">No UR data.</td></tr>'
    newer_count = sum(1 for listing in report.listings if listing.is_newer_building)
    ur_target_count = len(ur_alert_listings(report))
    status = (
        "Target housing found"
        if report.target_found
        else (
            f"Newer listing found ({report.newer_than_year}+)"
            if newer_count
            else (
                "UR target found"
                if ur_target_count
                else ("Other non-excluded listing found" if report.visible_count else "No target yet")
            )
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JKK House Watcher</title>
<style>
body {{
  margin: 0;
  font-family: Arial, "Yu Gothic", "Meiryo", sans-serif;
  background: #f6f7f9;
  color: #172033;
}}
main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 24px; margin: 0 0 8px; }}
.meta {{ color: #596275; margin-bottom: 18px; }}
.status {{
  padding: 14px 16px;
  border-left: 5px solid #2d6cdf;
  background: white;
  margin-bottom: 18px;
}}
.status.target {{ border-color: #0f9d58; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #d8dde6; padding: 9px 10px; text-align: left; font-size: 14px; }}
th {{ background: #edf1f7; }}
tr.target {{ background: #e7f7ed; }}
tr.newer {{ background: #fff7db; }}
tr.excluded {{ color: #7b8495; background: #f2f2f2; }}
.hint {{ margin-top: 14px; color: #596275; font-size: 13px; }}
button {{
  border: 1px solid #1d5fc1;
  background: #256bd8;
  color: white;
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
}}
</style>
</head>
<body>
<main>
<h1>JKK House Watcher</h1>
<div class="meta">Checked at {html.escape(report.checked_at)} / JKK total: {report.total_count if report.total_count is not None else "unknown"}</div>
<div class="status {'target' if report.target_found or newer_count or ur_target_count else ''}">
  <strong>{html.escape(status)}</strong><br>
  Target: {html.escape(", ".join(report.target_names))}<br>
  Excluded: {html.escape(", ".join(report.exclude_names))} ({report.excluded_count})
</div>
<table>
<thead><tr><th>Status</th><th>Name</th><th>Area</th><th>Layout</th><th>Floor</th><th>Rent</th><th>Fee</th><th>Units</th><th>Built</th></tr></thead>
<tbody>
{table}
</tbody>
</table>
<h2>UR 北区</h2>
<table>
<thead><tr><th>Status</th><th>Name</th><th>Place</th><th>Vacant Rooms</th><th>Room Details</th></tr></thead>
<tbody>
{ur_table}
</tbody>
</table>
<p class="hint">This page is generated locally. Use the dashboard server for automatic refresh.</p>
</main>
</body>
</html>"""


LEGACY_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JKK House Watcher</title>
<style>
:root { color-scheme: light; }
body {
  margin: 0;
  font-family: Arial, "Yu Gothic", "Meiryo", sans-serif;
  background: #f5f7fb;
  color: #172033;
}
main { max-width: 1120px; margin: 0 auto; padding: 24px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
h1 { margin: 0; font-size: 24px; }
h2 { margin: 0 0 12px; font-size: 18px; }
.sub { color: #596275; margin-top: 4px; }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #1f5fbf;
  background: #256bd8;
  color: white;
  border-radius: 6px;
  min-height: 38px;
  padding: 0 14px;
  cursor: pointer;
}
button:disabled { opacity: .6; cursor: wait; }
.panel {
  background: white;
  border: 1px solid #dde3ed;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.status { border-left: 5px solid #2d6cdf; }
.status.target { border-left-color: #0f9d58; background: #f3fbf6; }
.status.warn { border-left-color: #d97706; }
.grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric { background: #f7f9fc; border: 1px solid #e0e6ef; border-radius: 6px; padding: 12px; }
.metric b { display: block; font-size: 22px; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: white; }
th, td { border: 1px solid #d8dde6; padding: 9px 10px; text-align: left; font-size: 14px; }
th { background: #edf1f7; }
tr.target { background: #e7f7ed; }
tr.newer { background: #fff7db; }
tr.excluded { color: #7b8495; background: #f2f2f2; }
.muted { color: #657084; }
.badge {
  display: inline-flex;
  align-items: center;
  border: 1px solid #cfd7e6;
  border-radius: 999px;
  padding: 2px 8px;
  margin: 1px 4px 1px 0;
  background: #f7f9fc;
  white-space: nowrap;
}
.badge.hot { border-color: #f2bf62; background: #fff6df; color: #7a4c00; }
.badge.ref { border-color: #a7c5ff; background: #eef5ff; color: #174f9f; }
a.export { color: #1f5fbf; text-decoration: none; font-size: 14px; }
a.export:hover { text-decoration: underline; }
@media (max-width: 760px) {
  header { align-items: flex-start; flex-direction: column; }
  .grid { grid-template-columns: 1fr 1fr; }
  main { padding: 16px; }
  table { display: block; overflow-x: auto; }
}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>JKK House Watcher</h1>
      <div class="sub">区部搜索 / 重点：コーシャハイム加賀、コーシャハイム田端テラス / 自适应刷新统计</div>
    </div>
    <div class="toolbar">
      <a class="export" href="/api/export/listings" target="_blank" rel="noreferrer">导出房源 JSON</a>
      <a class="export" href="/api/export/events" target="_blank" rel="noreferrer">导出事件 JSON</a>
      <button id="refresh">立即检查</button>
    </div>
  </header>
  <section id="status" class="panel status">读取中...</section>
  <section class="grid" id="metrics"></section>
  <section class="panel">
    <h2>今日重点命中</h2>
    <table>
      <thead><tr><th>来源</th><th>住宅/団地</th><th>房号</th><th>户型</th><th>面积</th><th>租金</th><th>状态</th><th>首次发现</th><th>持续</th><th>次数</th><th>标签</th><th>详情</th></tr></thead>
      <tbody id="priorityRows"><tr><td colspan="12" class="muted">暂无重点命中。</td></tr></tbody>
    </table>
  </section>
  <section class="panel">
    <h2>最新命中列表</h2>
    <table>
      <thead><tr><th>时间</th><th>事件</th><th>来源</th><th>住宅/団地</th><th>房号</th><th>户型</th><th>租金</th><th>状态</th></tr></thead>
      <tbody id="eventRows"><tr><td colspan="8" class="muted">暂无事件。</td></tr></tbody>
    </table>
  </section>
  <section class="panel">
    <h2>快速判断</h2>
    <table>
      <thead><tr><th>判断</th><th>来源</th><th>住宅/团地</th><th>房号</th><th>户型</th><th>面积</th><th>租金</th><th>共益费</th><th>状态</th><th>已持续</th><th>详情</th></tr></thead>
      <tbody id="judgementRows"><tr><td colspan="11" class="muted">暂无可判断房源。</td></tr></tbody>
    </table>
  </section>
  <section class="panel">
    <table>
      <thead><tr><th>状态</th><th>住宅名</th><th>地区</th><th>户型</th><th>面积</th><th>租金</th><th>共益费</th><th>户数</th><th>竣工/年份</th></tr></thead>
      <tbody id="rows"><tr><td colspan="9" class="muted">暂无数据。</td></tr></tbody>
    </table>
  </section>
  <section class="panel">
    <h2>UR 北区</h2>
    <table>
      <thead><tr><th>状态</th><th>団地名</th><th>地址</th><th>空室</th><th>房间详情</th></tr></thead>
      <tbody id="urRows"><tr><td colspan="5" class="muted">暂无 UR 数据。</td></tr></tbody>
    </table>
  </section>
  <section class="panel">
    <h2>高概率时间段统计</h2>
    <table>
      <thead><tr><th>时间段</th><th>出现非排除房源次数</th><th>总检查次数</th><th>出现率</th><th>最近出现时间</th><th>出现过的房源</th></tr></thead>
      <tbody id="statsRows"><tr><td colspan="6" class="muted">暂无统计。</td></tr></tbody>
    </table>
  </section>
  <p class="muted">面板每 10 分钟自动刷新。后台监控在历史高概率时间段会改为每 5 分钟检查一次。</p>
</main>
<script>
const refreshButton = document.getElementById("refresh");
const statusBox = document.getElementById("status");
const metrics = document.getElementById("metrics");
const priorityRows = document.getElementById("priorityRows");
const eventRows = document.getElementById("eventRows");
const judgementRows = document.getElementById("judgementRows");
const rows = document.getElementById("rows");
const urRows = document.getElementById("urRows");
const statsRows = document.getElementById("statsRows");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`;
}

function tagBadges(tags) {
  return (tags || []).map(tag => {
    const cls = tag.includes("赤羽台") || tag.includes("高优先级") || tag.includes("快速消失") ? "badge hot" : "badge";
    return `<span class="${cls}">${escapeHtml(tag)}</span>`;
  }).join("");
}

function sourceHint(item) {
  if (item.source === "UR") return `<span class="badge ref">参考向</span>`;
  if (item.source === "JKK") return `<span class="badge ref">建议手动登录确认</span>`;
  return "";
}

function renderLifecycle(summary) {
  const priority = summary?.today_priority || [];
  priorityRows.innerHTML = priority.length ? priority.map(item => {
    const tags = tagBadges(item.quick_tags);
    const detail = item.detail_url ? `<a href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">打开</a>` : "-";
    return `<tr class="${item.is_high_priority && item.is_present ? "target" : ""}">
      <td>${escapeHtml(item.source)}</td>
      <td>${escapeHtml(item.building_name)}</td>
      <td>${escapeHtml(item.room_no || "-")}</td>
      <td>${escapeHtml(item.layout)}</td>
      <td>${escapeHtml(item.area)}</td>
      <td>${escapeHtml(item.rent)}</td>
      <td>${escapeHtml(item.current_status)}</td>
      <td>${escapeHtml(item.first_seen_at || "-")}</td>
      <td>${escapeHtml(item.lifetime_minutes ?? 0)} 分</td>
      <td>${escapeHtml(item.appearance_count ?? 0)}</td>
      <td>${sourceHint(item)}${tags}</td>
      <td>${detail}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="12" class="muted">暂无重点命中。</td></tr>`;

  const events = summary?.latest_events || [];
  eventRows.innerHTML = events.length ? events.map(item => {
    return `<tr>
      <td>${escapeHtml(item.created_at || "-")}</td>
      <td>${escapeHtml(item.event_type)}</td>
      <td>${escapeHtml(item.source)}</td>
      <td>${escapeHtml(item.building_name)}</td>
      <td>${escapeHtml(item.room_no || "-")}</td>
      <td>${escapeHtml(item.layout || "-")}</td>
      <td>${escapeHtml(item.rent || "-")}</td>
      <td>${escapeHtml(item.current_status || "-")}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="8" class="muted">暂无事件。</td></tr>`;

  const active = summary?.records || [];
  judgementRows.innerHTML = active.length ? active.map(item => {
    const detail = item.detail_url ? `<a href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">打开</a>` : "-";
    const cls = item.is_high_priority ? "target" : "";
    return `<tr class="${cls}">
      <td>${sourceHint(item)}${tagBadges(item.quick_tags)}</td>
      <td>${escapeHtml(item.source)}</td>
      <td>${escapeHtml(item.building_name)}</td>
      <td>${escapeHtml(item.room_no || "-")}</td>
      <td>${escapeHtml(item.layout || "-")}</td>
      <td>${escapeHtml(item.area || "-")}</td>
      <td>${escapeHtml(item.rent || "-")}</td>
      <td>${escapeHtml(item.common_fee || "-")}</td>
      <td>${escapeHtml(item.current_status || "-")}</td>
      <td>${escapeHtml(item.lifetime_minutes ?? 0)} 分</td>
      <td>${detail}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="11" class="muted">暂无可判断房源。</td></tr>`;
}

function render(data) {
  const found = data.target_found;
  const newer = (data.listings || []).filter(item => item.is_newer_building);
  const ur = data.ur_report || null;
  const urListings = ur?.listings || [];
  const urTargets = urListings.filter(item => item.is_target && Number(item.room_count || 0) > 0);
  const alertFound = found || newer.length > 0 || urTargets.length > 0;
  statusBox.className = `panel status ${alertFound ? "target" : ""}`;
  statusBox.innerHTML = found
    ? `<strong>重点房源出现了。</strong><br>${escapeHtml(data.target_names.join(", "))}`
    : newer.length
      ? `<strong>${escapeHtml(data.newer_than_year || 2010)}年以后房源出现了。</strong><br>${escapeHtml(newer.map(item => item.name).join(", "))}`
    : urTargets.length
      ? `<strong>UR ヌーヴェル赤羽台 有空室。</strong><br>${escapeHtml(urTargets.map(item => `${item.name} ${item.room_count}户`).join(", "))}`
    : `<strong>重点房源暂未出现。</strong><br>已排除 ${escapeHtml(data.excluded_count)} 条：${escapeHtml(data.exclude_names.join(", "))}`;
  if (data.warning) statusBox.innerHTML += `<br><span class="muted">${escapeHtml(data.warning)}</span>`;
  if (ur?.warning) statusBox.innerHTML += `<br><span class="muted">UR: ${escapeHtml(ur.warning)}</span>`;

  metrics.innerHTML = [
    metric("检查时间", data.checked_at),
    metric("JKK总数", data.total_count ?? "未知"),
    metric("非排除房源", data.visible_count),
    metric(`${escapeHtml(data.newer_than_year || 2010)}+房源`, newer.length),
    metric("UR北区空室", ur?.total_vacancies ?? "-"),
    metric("UR赤羽台", urTargets.length ? "有" : "无"),
    metric("事件提频", data.lifecycle_summary?.boost?.active ? "1分钟" : "普通"),
    metric("已排除", data.excluded_count),
  ].join("");

  renderLifecycle(data.lifecycle_summary);

  const listings = data.listings || [];
  rows.innerHTML = listings.length ? listings.map(item => {
    const status = item.is_target ? "重点" : (item.is_newer_building ? "2010+" : (item.is_excluded ? "已排除" : "候选"));
    const cls = item.is_target ? "target" : (item.is_newer_building ? "newer" : (item.is_excluded ? "excluded" : ""));
    const year = item.building_year_label || item.building_year || (item.detail_error ? "读取失败" : "");
    return `<tr class="${cls}">
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(item.name)}</td>
      <td>${escapeHtml(item.area)}</td>
      <td>${escapeHtml(item.layout)}</td>
      <td>${escapeHtml(item.floor_area_m2)}</td>
      <td>${escapeHtml(item.rent_yen)}</td>
      <td>${escapeHtml(item.common_fee_yen)}</td>
      <td>${escapeHtml(item.units)}</td>
      <td>${escapeHtml(year)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="9" class="muted">暂无房源。</td></tr>`;

  urRows.innerHTML = urListings.length ? urListings.map(item => {
    const isUrTarget = item.is_target && Number(item.room_count || 0) > 0;
    const status = isUrTarget ? "赤羽台" : "UR";
    const cls = isUrTarget ? "target" : "";
    const roomDetails = (item.rooms || []).map(room => {
      const tags = tagBadges(room.quick_tags);
      return [tags, room.current_status, room.building, room.room_no, room.layout, room.floor_area_m2, room.floor, room.rent_yen, room.common_fee_yen ? `共益費 ${room.common_fee_yen}` : ""]
        .filter(Boolean)
        .map(value => String(value).startsWith("<span") ? value : escapeHtml(value))
        .join(" / ");
    }).join("<br>");
    return `<tr class="${cls}">
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(item.name)}</td>
      <td>${escapeHtml(item.place)}</td>
      <td>${escapeHtml(item.room_count)}</td>
      <td>${roomDetails || "-"}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="muted">暂无 UR 数据。</td></tr>`;
}

function renderStats(data) {
  const hot = data.hot_buckets || [];
  if (!hot.length) {
    statsRows.innerHTML = `<tr><td colspan="6" class="muted">还没有出现过非排除房源。后台监控会保持 10 分钟检查一次。</td></tr>`;
    return;
  }
  statsRows.innerHTML = hot.map(item => {
    const rate = `${((item.candidate_rate || 0) * 100).toFixed(1)}%`;
    const seenListings = (item.candidate_seen || []).map(record => {
      const year = record.building_year_label || record.building_year || "";
      const parts = [record.name, record.area, record.layout, record.rent_yen, year, `累计出现 ${record.seen_count || 0} 次`].filter(Boolean);
      return parts.map(escapeHtml).join(" / ");
    }).join("<br>");
    return `<tr>
      <td>${escapeHtml(item.bucket)}</td>
      <td>${escapeHtml(item.candidate_checks)}</td>
      <td>${escapeHtml(item.checks)}</td>
      <td>${escapeHtml(rate)}</td>
      <td>${escapeHtml(item.last_candidate_at || "-")}</td>
      <td>${seenListings || "-"}</td>
    </tr>`;
  }).join("");
}

async function refreshStats() {
  const response = await fetch("/api/stats", { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  renderStats(data);
}

async function checkNow() {
  refreshButton.disabled = true;
  refreshButton.textContent = "检查中...";
  try {
    const response = await fetch("/api/check", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    render(data);
    await refreshStats();
  } catch (error) {
    statusBox.className = "panel status warn";
    statusBox.innerHTML = `<strong>检查失败。</strong><br>${escapeHtml(error.message)}`;
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "立即检查";
  }
}

refreshButton.addEventListener("click", checkNow);
checkNow();
setInterval(checkNow, 10 * 60 * 1000);
</script>
</body>
</html>
"""


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JKK House Watcher</title>
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Arial, "Yu Gothic", "Meiryo", sans-serif;
  background: #f4f6f9;
  color: #172033;
}
main { max-width: 1180px; margin: 0 auto; padding: 20px 24px 32px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
h1 { margin: 0; font-size: 22px; }
h2 { margin: 0; font-size: 17px; }
.sub { color: #687386; margin-top: 4px; font-size: 13px; }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #1f5fbf;
  background: #256bd8;
  color: white;
  border-radius: 6px;
  min-height: 34px;
  padding: 0 12px;
  cursor: pointer;
}
button.secondary { border-color: #c7d0df; background: white; color: #24324a; }
button:disabled { opacity: .6; cursor: wait; }
a { color: #1f5fbf; text-decoration: none; }
a:hover { text-decoration: underline; }
a.export { font-size: 13px; }
.panel {
  background: white;
  border: 1px solid #dde3ed;
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 12px;
}
.action-summary {
  display: grid;
  grid-template-columns: minmax(220px, 1.4fr) repeat(5, minmax(112px, 1fr));
  gap: 10px;
  align-items: stretch;
  border-left: 5px solid #c7d0df;
}
.action-summary.has-akabane { border-left-color: #d93025; background: #fff7f6; }
.action-summary.has-jkk { border-left-color: #256bd8; background: #f5f9ff; }
.summary-main { display: flex; flex-direction: column; gap: 6px; }
.summary-title { font-size: 19px; font-weight: 700; }
.summary-note { color: #687386; font-size: 13px; }
.summary-cell {
  border: 1px solid #e3e8f0;
  border-radius: 7px;
  padding: 9px 10px;
  background: rgba(255,255,255,.74);
}
.summary-cell span, .metric span { display: block; color: #687386; font-size: 12px; }
.summary-cell b, .metric b { display: block; margin-top: 3px; font-size: 16px; }
.metric-row { display: grid; grid-template-columns: 1.4fr repeat(4, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px; }
.metric { background: white; border: 1px solid #dde3ed; border-radius: 8px; padding: 9px 10px; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: separate; border-spacing: 0; background: white; }
th, td { border-bottom: 1px solid #e5eaf2; padding: 9px 10px; text-align: left; font-size: 13px; vertical-align: top; }
th { color: #526073; background: #f7f9fc; font-weight: 700; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
.row-akabane { background: #fff2f1; box-shadow: inset 4px 0 #d93025; }
.row-jkk { background: #f2f7ff; box-shadow: inset 4px 0 #256bd8; }
.row-ur { background: #f7f9fc; }
.row-warning { background: #fff7e6; }
.badge {
  display: inline-flex;
  align-items: center;
  border: 1px solid #d5dce8;
  border-radius: 999px;
  padding: 1px 6px;
  margin: 1px 3px 1px 0;
  background: #f8fafc;
  color: #48556a;
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
}
.badge.hot { border-color: #d93025; background: #fff1f0; color: #a32018; }
.badge.jkk { border-color: #8eb7ff; background: #eef5ff; color: #174f9f; }
.badge.ur { border-color: #c5cedd; background: #f4f7fb; color: #506078; }
.status-pill {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
  background: #eef1f6;
  color: #48556a;
  white-space: nowrap;
}
.status-pill.new, .status-pill.reappeared { background: #eaf5ef; color: #137333; }
.status-pill.stable { background: #eef1f6; color: #48556a; }
.status-pill.disappeared_fast { background: #fff1df; color: #9a4d00; }
.muted { color: #687386; }
.empty { padding: 10px 2px; color: #687386; }
details.panel { padding: 0; }
details.panel > summary {
  cursor: pointer;
  list-style: none;
  padding: 14px;
  font-weight: 700;
}
details.panel > summary::-webkit-details-marker { display: none; }
.details-body { border-top: 1px solid #e5eaf2; padding: 12px 14px 14px; }
.toggle-line { display: inline-flex; align-items: center; gap: 6px; margin-bottom: 10px; color: #526073; font-size: 13px; }
.compact-note { color: #687386; font-size: 13px; }
@media (max-width: 860px) {
  header { align-items: flex-start; flex-direction: column; }
  .action-summary { grid-template-columns: 1fr 1fr; }
  .summary-main { grid-column: 1 / -1; }
  .metric-row { grid-template-columns: 1fr 1fr; }
  main { padding: 16px; }
}
@media (max-width: 560px) {
  .action-summary, .metric-row { grid-template-columns: 1fr; }
  th, td { padding: 8px; }
}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>JKK House Watcher</h1>
      <div class="sub">JKK 区部 + UR 北区 / 首页只显示行动优先级</div>
    </div>
    <div class="toolbar">
      <a class="export" href="/api/export/listings" target="_blank" rel="noreferrer">导出房源 JSON</a>
      <a class="export" href="/api/export/events" target="_blank" rel="noreferrer">导出事件 JSON</a>
      <button id="refresh">立即检查</button>
    </div>
  </header>

  <section id="status" class="panel action-summary">读取中...</section>
  <section class="metric-row" id="metrics"></section>

  <section class="panel">
    <div class="section-head">
      <h2>当前值得关注</h2>
      <span class="compact-note" id="focusHint">只看仍然存在的房源</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>来源</th><th>团地名</th><th>房号</th><th>户型</th><th>面积</th><th>租金</th><th>共益费</th><th>状态</th><th>已持续</th><th>标签</th><th>详情</th></tr></thead>
        <tbody id="focusRows"><tr><td colspan="11" class="muted">暂无当前值得关注房源。</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <div class="section-head">
      <h2>最新事件流</h2>
      <button class="secondary" id="eventToggle" type="button">展开更多</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>事件</th><th>来源</th><th>团地名</th><th>房号</th><th>户型</th><th>租金</th><th>状态</th></tr></thead>
        <tbody id="eventRows"><tr><td colspan="8" class="muted">暂无事件。</td></tr></tbody>
      </table>
    </div>
  </section>

  <details class="panel" id="urDetails">
    <summary id="urSummary">UR北区总览</summary>
    <div class="details-body">
      <label class="toggle-line"><input type="checkbox" id="showAllUr"> 显示全部団地</label>
      <div class="table-wrap">
        <table>
          <thead><tr><th>状态</th><th>团地名</th><th>地址</th><th>空室</th><th>房间摘要</th></tr></thead>
          <tbody id="urRows"><tr><td colspan="5" class="muted">暂无 UR 数据。</td></tr></tbody>
        </table>
      </div>
    </div>
  </details>

  <details class="panel">
    <summary>高概率时段统计</summary>
    <div class="details-body">
      <div class="table-wrap">
        <table>
          <thead><tr><th>时间段</th><th>出现次数</th><th>总检查</th><th>出现率</th><th>最近出现</th><th>出现过的房源</th></tr></thead>
          <tbody id="statsRows"><tr><td colspan="6" class="muted">暂无统计。</td></tr></tbody>
        </table>
      </div>
    </div>
  </details>

  <details class="panel">
    <summary>原始 JKK 列表</summary>
    <div class="details-body">
      <div class="table-wrap">
        <table>
          <thead><tr><th>状态</th><th>住宅名</th><th>地区</th><th>户型</th><th>面积</th><th>租金</th><th>共益费</th><th>户数</th><th>竣工/年份</th></tr></thead>
          <tbody id="rows"><tr><td colspan="9" class="muted">暂无数据。</td></tr></tbody>
        </table>
      </div>
    </div>
  </details>

  <p class="muted">后台仍按原规则运行：普通 10 分钟，高概率时段 5 分钟，事件提频时 1 分钟。</p>
</main>
<script>
const refreshButton = document.getElementById("refresh");
const statusBox = document.getElementById("status");
const metrics = document.getElementById("metrics");
const focusRows = document.getElementById("focusRows");
const focusHint = document.getElementById("focusHint");
const eventRows = document.getElementById("eventRows");
const eventToggle = document.getElementById("eventToggle");
const rows = document.getElementById("rows");
const urRows = document.getElementById("urRows");
const urSummary = document.getElementById("urSummary");
const showAllUr = document.getElementById("showAllUr");
const statsRows = document.getElementById("statsRows");
let eventsExpanded = false;
let latestEventView = [];
let latestUrListings = [];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`;
}

function isAkabane(item) {
  return String(item?.building_name || item?.name || "").includes("赤羽台");
}

function isHighPriorityJkk(item) {
  return item?.source === "JKK" && item?.is_high_priority;
}

function statusPill(status) {
  const value = status || "-";
  return `<span class="status-pill ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
}

function rowClass(item) {
  if (isAkabane(item)) return "row-akabane";
  if (isHighPriorityJkk(item)) return "row-jkk";
  if (item?.current_status === "disappeared_fast") return "row-warning";
  if (item?.source === "UR") return "row-ur";
  return "";
}

function focusSort(a, b) {
  const score = item => {
    if (isAkabane(item)) return 0;
    if (isHighPriorityJkk(item)) return 1;
    if (item?.is_high_priority) return 2;
    return 3;
  };
  const diff = score(a) - score(b);
  if (diff) return diff;
  return String(b.last_seen_at || "").localeCompare(String(a.last_seen_at || ""));
}

function compactTags(item) {
  const candidates = [];
  const quick = item?.quick_tags || [];
  if (isAkabane(item)) candidates.push(["赤羽台", "hot"]);
  if (item?.is_high_priority) candidates.push(["高优先级", item.source === "JKK" ? "jkk" : "hot"]);
  if (item?.source === "JKK") candidates.push(["JKK手动确认", "jkk"]);
  if (item?.source === "UR") candidates.push(["UR参考", "ur"]);
  for (const tag of quick) {
    if (["低价", "大户型", "快速消失"].includes(tag)) {
      candidates.push([tag, tag === "快速消失" ? "hot" : ""]);
    }
  }
  const seen = new Set();
  return candidates
    .filter(([label]) => {
      if (seen.has(label)) return false;
      seen.add(label);
      return true;
    })
    .slice(0, 2)
    .map(([label, cls]) => `<span class="badge ${cls}">${escapeHtml(label)}</span>`)
    .join("");
}

function significantEvents(events) {
  const allowed = new Set(["first_seen", "stable", "reappeared", "disappeared_fast"]);
  const seenStable = new Set();
  return (events || []).filter(event => {
    if (!allowed.has(event.event_type)) return false;
    if (event.event_type === "stable") {
      const key = event.stable_id || `${event.source}:${event.building_name}:${event.room_no}`;
      if (seenStable.has(key)) return false;
      seenStable.add(key);
    }
    return true;
  });
}

function withinLast24Hours(value, checkedAt) {
  const base = Date.parse(checkedAt || new Date().toISOString());
  const seen = Date.parse(value || "");
  if (!Number.isFinite(base) || !Number.isFinite(seen)) return false;
  return base - seen >= 0 && base - seen <= 24 * 60 * 60 * 1000;
}

function renderActionSummary(data, active) {
  const summary = data.lifecycle_summary || {};
  const events = significantEvents(summary.latest_events || []);
  const akabaneActive = active.filter(isAkabane);
  const highJkkActive = active.filter(isHighPriorityJkk);
  const boost = summary.boost || {};
  const todayNew = events.filter(event => ["first_seen", "reappeared"].includes(event.event_type) && withinLast24Hours(event.created_at, data.checked_at)).length;
  const title = akabaneActive.length
    ? "赤羽台出现"
    : highJkkActive.length
      ? "高优先级 JKK 出现"
      : "当前无赤羽台命中";
  const best = active[0];
  const note = best
    ? `当前最值得关注：${best.building_name || "-"}`
    : "当前没有仍在的重点候选";
  statusBox.className = `panel action-summary ${akabaneActive.length ? "has-akabane" : highJkkActive.length ? "has-jkk" : ""}`;
  statusBox.innerHTML = `
    <div class="summary-main">
      <div class="summary-title">${escapeHtml(title)}</div>
      <div class="summary-note">${escapeHtml(note)}</div>
    </div>
    <div class="summary-cell"><span>赤羽台</span><b>${akabaneActive.length ? "有" : "无"}</b></div>
    <div class="summary-cell"><span>高优先级 JKK</span><b>${highJkkActive.length ? "有" : "无"}</b></div>
    <div class="summary-cell"><span>当前提频</span><b>${boost.active ? "提频中" : "普通"}</b></div>
    <div class="summary-cell"><span>今日新命中</span><b>${escapeHtml(todayNew)}</b></div>
    <div class="summary-cell"><span>今日仍在</span><b>${escapeHtml(active.length)}</b></div>
  `;
  if (data.warning || data.ur_report?.warning) {
    statusBox.innerHTML += `<div class="summary-note">${escapeHtml(data.warning || data.ur_report.warning)}</div>`;
  }
}

function renderMetrics(data, active) {
  const ur = data.ur_report || {};
  const akabaneActive = active.some(isAkabane);
  metrics.innerHTML = [
    metric("检查时间", data.checked_at || "-"),
    metric("JKK当前数", data.total_count ?? "未知"),
    metric("UR北区空室", ur.total_vacancies ?? "-"),
    metric("赤羽台", akabaneActive ? "有" : "无"),
    metric("提频", data.lifecycle_summary?.boost?.active ? "提频中" : "普通"),
  ].join("");
}

function renderFocus(summary) {
  const active = [...(summary?.records || [])].sort(focusSort);
  focusHint.textContent = active.length ? `当前仍在 ${active.length} 套` : "当前没有仍在房源";
  focusRows.innerHTML = active.length ? active.map(item => {
    const detail = item.detail_url ? `<a href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">打开</a>` : "-";
    return `<tr class="${rowClass(item)}">
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${escapeHtml(item.building_name || "-")}</td>
      <td>${escapeHtml(item.room_no || "-")}</td>
      <td>${escapeHtml(item.layout || "-")}</td>
      <td>${escapeHtml(item.area || "-")}</td>
      <td>${escapeHtml(item.rent || "-")}</td>
      <td>${escapeHtml(item.common_fee || "-")}</td>
      <td>${statusPill(item.current_status)}</td>
      <td>${escapeHtml(item.lifetime_minutes ?? 0)} 分</td>
      <td>${compactTags(item)}</td>
      <td>${detail}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="11" class="empty">当前无赤羽台命中，也没有其他仍在房源。</td></tr>`;
  return active;
}

function renderEvents(events) {
  latestEventView = significantEvents(events);
  const shown = eventsExpanded ? latestEventView.slice(0, 30) : latestEventView.slice(0, 5);
  eventToggle.style.display = latestEventView.length > 5 ? "inline-flex" : "none";
  eventToggle.textContent = eventsExpanded ? "折叠" : "展开更多";
  eventRows.innerHTML = shown.length ? shown.map(item => {
    return `<tr class="${item.event_type === "disappeared_fast" ? "row-warning" : ""}">
      <td>${escapeHtml(item.created_at || "-")}</td>
      <td>${escapeHtml(item.event_type)}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${escapeHtml(item.building_name || "-")}</td>
      <td>${escapeHtml(item.room_no || "-")}</td>
      <td>${escapeHtml(item.layout || "-")}</td>
      <td>${escapeHtml(item.rent || "-")}</td>
      <td>${statusPill(item.current_status)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="8" class="empty">暂无 first_seen / stable / reappeared / disappeared_fast 事件。</td></tr>`;
}

function roomSummary(room) {
  const parts = [room.building, room.room_no, room.layout, room.floor_area_m2, room.rent_yen, room.common_fee_yen ? `共益費 ${room.common_fee_yen}` : ""].filter(Boolean);
  const link = room.detail_url ? ` <a href="${escapeHtml(room.detail_url)}" target="_blank" rel="noreferrer">打开</a>` : "";
  return `${parts.map(escapeHtml).join(" / ")}${link}`;
}

function renderUr() {
  const visible = showAllUr.checked
    ? latestUrListings
    : latestUrListings.filter(item => Number(item.room_count || 0) > 0);
  const activeBuildings = latestUrListings.filter(item => Number(item.room_count || 0) > 0).length;
  urSummary.textContent = `UR北区总览（当前有空室 ${activeBuildings} 个団地）`;
  urRows.innerHTML = visible.length ? visible.map(item => {
    const isTarget = item.is_target && Number(item.room_count || 0) > 0;
    const rooms = item.rooms || [];
    const shortRooms = rooms.slice(0, 2).map(roomSummary).join("<br>");
    const more = rooms.length > 2 ? `<br><span class="muted">另有 ${rooms.length - 2} 间，打开详情查看</span>` : "";
    return `<tr class="${isTarget ? "row-akabane" : Number(item.room_count || 0) > 0 ? "row-ur" : ""}">
      <td>${isTarget ? "赤羽台" : Number(item.room_count || 0) > 0 ? "有空室" : "无空室"}</td>
      <td>${escapeHtml(item.name || "-")}</td>
      <td>${escapeHtml(item.place || "-")}</td>
      <td>${escapeHtml(item.room_count ?? 0)}</td>
      <td>${shortRooms || "-"}${more}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">当前没有空室団地。勾选“显示全部団地”可查看完整列表。</td></tr>`;
}

function renderRawJkk(data) {
  const listings = data.listings || [];
  rows.innerHTML = listings.length ? listings.map(item => {
    const status = item.is_target ? "重点" : (item.is_newer_building ? "2010+" : (item.is_excluded ? "已排除" : "候选"));
    const cls = item.is_target ? "row-jkk" : (item.is_newer_building ? "row-warning" : (item.is_excluded ? "muted" : ""));
    const year = item.building_year_label || item.building_year || (item.detail_error ? "读取失败" : "");
    return `<tr class="${cls}">
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(item.name)}</td>
      <td>${escapeHtml(item.area)}</td>
      <td>${escapeHtml(item.layout)}</td>
      <td>${escapeHtml(item.floor_area_m2)}</td>
      <td>${escapeHtml(item.rent_yen)}</td>
      <td>${escapeHtml(item.common_fee_yen)}</td>
      <td>${escapeHtml(item.units)}</td>
      <td>${escapeHtml(year)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="9" class="empty">暂无 JKK 房源。</td></tr>`;
}

function render(data) {
  const summary = data.lifecycle_summary || {};
  latestUrListings = data.ur_report?.listings || [];
  const active = renderFocus(summary);
  renderActionSummary(data, active);
  renderMetrics(data, active);
  renderEvents(summary.latest_events || []);
  renderUr();
  renderRawJkk(data);
}

function renderStats(data) {
  const hot = data.hot_buckets || [];
  if (!hot.length) {
    statsRows.innerHTML = `<tr><td colspan="6" class="empty">还没有出现过非排除房源。后台监控会保持 10 分钟检查一次。</td></tr>`;
    return;
  }
  statsRows.innerHTML = hot.map(item => {
    const rate = `${((item.candidate_rate || 0) * 100).toFixed(1)}%`;
    const seenListings = (item.candidate_seen || []).map(record => {
      const year = record.building_year_label || record.building_year || "";
      const parts = [record.name, record.area, record.layout, record.rent_yen, year, `累计出现 ${record.seen_count || 0} 次`].filter(Boolean);
      return parts.map(escapeHtml).join(" / ");
    }).join("<br>");
    return `<tr>
      <td>${escapeHtml(item.bucket)}</td>
      <td>${escapeHtml(item.candidate_checks)}</td>
      <td>${escapeHtml(item.checks)}</td>
      <td>${escapeHtml(rate)}</td>
      <td>${escapeHtml(item.last_candidate_at || "-")}</td>
      <td>${seenListings || "-"}</td>
    </tr>`;
  }).join("");
}

async function refreshStats() {
  const response = await fetch("/api/stats", { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  renderStats(data);
}

async function checkNow() {
  refreshButton.disabled = true;
  refreshButton.textContent = "检查中...";
  try {
    const response = await fetch("/api/check", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    render(data);
    await refreshStats();
  } catch (error) {
    statusBox.className = "panel action-summary";
    statusBox.innerHTML = `<div class="summary-main"><div class="summary-title">检查失败</div><div class="summary-note">${escapeHtml(error.message)}</div></div>`;
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "立即检查";
  }
}

eventToggle.addEventListener("click", () => {
  eventsExpanded = !eventsExpanded;
  renderEvents(latestEventView);
});
showAllUr.addEventListener("change", renderUr);
refreshButton.addEventListener("click", checkNow);
checkNow();
setInterval(checkNow, 10 * 60 * 1000);
</script>
</body>
</html>
"""


def get_runtime_rules(args: argparse.Namespace) -> dict:
    rules = getattr(args, "rules", None)
    if not rules:
        rules = load_watch_rules(getattr(args, "rules_file", DEFAULT_RULES_FILE))
        args.rules = rules
    return rules


def build_check_report(args: argparse.Namespace) -> CheckReport:
    rules = get_runtime_rules(args)
    base_dir = Path(getattr(args, "data_dir", DEFAULT_ALERT_DIR))
    stale_seconds = int(rules.get("fetch_lock_stale_seconds", 300))
    with FetchLock(base_dir, stale_seconds=stale_seconds):
        return build_check_report_unlocked(args, rules, base_dir)


def build_check_report_unlocked(
    args: argparse.Namespace,
    rules: dict,
    base_dir: Path,
) -> CheckReport:
    report = JkkClient(timeout=args.timeout, verify_ssl=args.verify_ssl).search_wards(
        targets=args.target,
        excludes=args.exclude,
        name_kana=args.name_kana,
        read_detail_years=not args.no_detail_years,
        newer_than_year=args.newer_than_year,
    )
    if not args.no_ur:
        try:
            report.ur_report = UrClient(timeout=args.timeout).search_kita(
                targets=args.ur_target,
                source_url=args.ur_url,
            )
        except Exception as error:
            report.ur_report = UrReport(
                checked_at=datetime.now().isoformat(timespec="seconds"),
                source_url=args.ur_url,
                target_names=args.ur_target,
                target_found=False,
                total_properties=0,
                total_vacancies=0,
                listings=[],
                warning=str(error),
            )
    records = lifecycle_records_from_report(report, rules)
    snapshot_meta = {
        "checked_at": report.checked_at,
        "jkk_total": report.total_count,
        "jkk_visible_count": report.visible_count,
        "jkk_excluded_count": report.excluded_count,
        "ur_total_properties": report.ur_report.total_properties if report.ur_report else 0,
        "ur_total_vacancies": report.ur_report.total_vacancies if report.ur_report else 0,
        "source_urls": {
            "jkk": report.source_url,
            "ur": report.ur_report.source_url if report.ur_report else "",
        },
    }
    store = ListingLifecycleStore(base_dir, rules)
    report.lifecycle_summary = store.update(records, snapshot_meta)
    apply_lifecycle_summary(report, report.lifecycle_summary)
    return report


def run_once(args: argparse.Namespace) -> int:
    report = build_check_report(args)
    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report, include_excluded=args.include_excluded))

    if args.output_html:
        with open(args.output_html, "w", encoding="utf-8") as file:
            file.write(render_html_report(report))
        print(f"\nHTML report: {args.output_html}")
    if args.popup and lifecycle_alert_records(report):
        popup(report)
    if args.beep and lifecycle_alert_records(report):
        beep()
    return 0 if not args.fail_when_missing or build_target_signature(report) else 2


def run_watch(args: argparse.Namespace) -> int:
    mode = (
        f"adaptive {args.interval}/{args.fast_interval} seconds"
        if args.adaptive
        else f"{args.interval} seconds"
    )
    print(f"Watching JKK/UR every {mode}. Press Ctrl+C to stop.")
    last_signature = None
    last_target_signature = None
    try:
        while True:
            sleep_seconds = args.interval
            try:
                report = build_check_report(args)
                stats = update_stats(report, args.stats_file, args.stats_bucket_minutes)
                signature = json.dumps(report_to_dict(report), ensure_ascii=False, sort_keys=True)
                if signature != last_signature or build_target_signature(report):
                    print("\n" + format_report(report, include_excluded=args.include_excluded))
                    last_signature = signature
                target_signature = build_target_signature(report)
                write_found = bool(target_signature and target_signature != last_target_signature)
                write_watch_files(report, args.alert_dir, args.include_excluded, write_found)
                has_popup_alerts = bool(lifecycle_alert_records(report))
                if target_signature:
                    if has_popup_alerts and args.popup:
                        popup(report)
                    last_target_signature = target_signature
                else:
                    last_target_signature = None
                if has_popup_alerts and args.beep:
                    beep()
                boost = (report.lifecycle_summary or {}).get("boost", {})
                if boost.get("active"):
                    sleep_seconds = min(sleep_seconds, int(boost.get("interval_seconds") or 60))
                    print(f"Event boost: next check in {sleep_seconds} seconds until {boost.get('until')}.")
                elif args.adaptive and is_hot_bucket(stats, bucket_minutes=args.stats_bucket_minutes):
                    sleep_seconds = args.fast_interval
                    print(f"Adaptive refresh: current hour is hot, next check in {sleep_seconds} seconds.")
                elif args.adaptive:
                    print(f"Adaptive refresh: normal period, next check in {sleep_seconds} seconds.")
            except Exception as error:  # keep the monitor alive through transient site failures
                print(f"\n{datetime.now().isoformat(timespec='seconds')} check failed: {error}", file=sys.stderr)
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def build_target_signature(report: CheckReport) -> str:
    if report.lifecycle_summary:
        targets = [
            {
                "stable_id": record.get("stable_id"),
                "source": record.get("source"),
                "building_name": record.get("building_name"),
                "room_no": record.get("room_no"),
                "layout": record.get("layout"),
                "area": record.get("area"),
                "rent": record.get("rent"),
                "common_fee": record.get("common_fee"),
                "current_status": record.get("current_status"),
                "appearance_count": record.get("appearance_count"),
            }
            for record in report.lifecycle_summary.get("records", [])
            if record.get("is_high_priority") and record.get("is_present")
        ]
        if not targets:
            return ""
        return json.dumps(targets, ensure_ascii=False, sort_keys=True)

    targets = {
        "jkk": [
            {
                "marker": listing_marker(listing),
                "name": listing.name,
                "area": listing.area,
                "layout": listing.layout,
                "floor_area_m2": listing.floor_area_m2,
                "rent_yen": listing.rent_yen,
                "units": listing.units,
                "building_year": listing.building_year,
                "building_year_label": listing.building_year_label,
            }
            for listing in report.listings
            if listing.is_target or listing.is_newer_building
        ],
        "ur": [
            {
                "name": listing.name,
                "place": listing.place,
                "room_count": listing.room_count,
                "rooms": [asdict(room) for room in (listing.rooms or [])],
            }
            for listing in ur_alert_listings(report)
        ],
    }
    if not targets["jkk"] and not targets["ur"]:
        return ""
    return json.dumps(targets, ensure_ascii=False, sort_keys=True)


def build_jkk_target_payload(report: CheckReport) -> list[dict]:
    return [
        {
            "marker": listing_marker(listing),
            "name": listing.name,
            "area": listing.area,
            "layout": listing.layout,
            "floor_area_m2": listing.floor_area_m2,
            "rent_yen": listing.rent_yen,
            "units": listing.units,
            "building_year": listing.building_year,
            "building_year_label": listing.building_year_label,
        }
        for listing in report.listings
        if listing.is_target or listing.is_newer_building
    ]


def write_watch_files(
    report: CheckReport,
    alert_dir: str,
    include_excluded: bool,
    write_found: bool,
) -> None:
    out_dir = Path(alert_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    latest_json = out_dir / "watch_latest.json"
    latest_html = out_dir / "watch_latest.html"
    latest_json.write_text(
        json.dumps(report_to_dict(report, include_excluded=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_html.write_text(render_html_report(report), encoding="utf-8")

    if not write_found:
        return

    text = format_report(report, include_excluded=include_excluded)
    found_latest = out_dir / "FOUND_latest.txt"
    found_latest.write_text(text + "\n", encoding="utf-8")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    found_snapshot = out_dir / f"FOUND_{timestamp}.txt"
    found_snapshot.write_text(text + "\n", encoding="utf-8")

    history_line = (
        f"{report.checked_at}\tALERT\t"
        + "; ".join(
            f"{listing_marker(listing)} / {listing.name} / {listing.layout} / {listing.rent_yen} / "
            f"{listing_year_text(listing) or 'year unknown'} / units {listing.units}"
            for listing in report.listings
            if listing.is_target or listing.is_newer_building
        )
        + ("; " if alert_listings(report) and ur_alert_listings(report) else "")
        + "; ".join(
            f"UR TARGET / {ur_listing_summary(listing)}"
            for listing in ur_alert_listings(report)
        )
        + "\n"
    )
    with (out_dir / "watch_history.log").open("a", encoding="utf-8") as file:
        file.write(history_line)


def lifecycle_popup_lines(record: dict) -> list[str]:
    lifetime = record.get("lifetime_minutes", 0)
    status = record.get("current_status") or "-"
    tags = ", ".join(record.get("quick_tags") or [])
    if record.get("source") == "UR":
        header = "UR 发现参考房源，请关注户型/价格/楼层；如需行动，请电话或线下确认。"
    else:
        header = "JKK 发现新房，请尽快手动登录官网确认。"
    detail = " / ".join(
        value
        for value in [
            record.get("building_name", ""),
            record.get("room_no", ""),
            record.get("layout", ""),
            record.get("area", ""),
            record.get("rent", ""),
            f"共益费 {record.get('common_fee')}" if record.get("common_fee") else "",
        ]
        if value
    )
    return [
        header,
        detail,
        f"首次出现: {record.get('first_seen_at') or '-'}",
        f"已持续: {lifetime} 分钟",
        f"状态: {status}",
        f"标签: {tags}" if tags else "",
        f"详情: {record.get('detail_url')}" if record.get("detail_url") else "",
        "",
    ]


def mark_popup_alerts_sent(report: CheckReport, records: list[dict]) -> None:
    if not report.lifecycle_summary or not records:
        return
    alerts_path = (
        report.lifecycle_summary.get("data_files", {}).get("alerts")
        if report.lifecycle_summary
        else None
    )
    if not alerts_path:
        return
    path = Path(alerts_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    for record in records:
        stable_id = record.get("stable_id")
        if not stable_id:
            continue
        data[stable_id] = {
            "last_alert_at": report.checked_at,
            "last_status": record.get("current_status"),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def popup(report: CheckReport) -> None:
    lifecycle_alerts = lifecycle_alert_records(report)
    if lifecycle_alerts:
        lines = ["房源提醒", ""]
        for record in lifecycle_alerts:
            lines.extend(line for line in lifecycle_popup_lines(record) if line)
        lines.extend(
            [
                f"Checked: {report.checked_at}",
                "Dashboard: http://127.0.0.1:8765/",
            ]
        )
        message = "\n".join(lines)
        title = "House Watcher"
        show_popup_message(title, message)
        mark_popup_alerts_sent(report, lifecycle_alerts)
        return

    rows = alert_listings(report)
    ur_rows = ur_alert_listings(report)
    if not rows and not ur_rows:
        return

    lines = [
        "房源提醒",
        "",
        *[
            f"{listing.name} / {listing.area} / {listing.layout} / {listing.rent_yen} / {listing.units}戸"
            for listing in rows
        ],
        *[
            f"Built: {listing.name} / {listing_year_text(listing)}"
            for listing in rows
            if listing_year_text(listing)
        ],
        *(
            ["", "UR ヌーヴェル赤羽台 有空室"]
            if ur_rows
            else []
        ),
        *[ur_listing_summary(listing) for listing in ur_rows],
        "",
        f"Checked: {report.checked_at}",
        "Dashboard: http://127.0.0.1:8765/",
    ]
    message = "\n".join(lines)
    title = "JKK House Watcher"

    show_popup_message(title, message)


def show_popup_message(title: str, message: str) -> None:

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = "\n".join(
        [
            "Add-Type -AssemblyName PresentationFramework",
            f"$title = {ps_quote(title)}",
            f"$message = {ps_quote(message)}",
            "$owner = New-Object System.Windows.Window",
            "$owner.Topmost = $true",
            "$owner.ShowInTaskbar = $false",
            "$owner.WindowStyle = [System.Windows.WindowStyle]::None",
            "$owner.ResizeMode = [System.Windows.ResizeMode]::NoResize",
            "$owner.Width = 1",
            "$owner.Height = 1",
            "$owner.Left = -10000",
            "$owner.Top = -10000",
            "$owner.Show()",
            "$owner.Activate() | Out-Null",
            "[System.Windows.MessageBox]::Show($owner, $message, $title, [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::None) | Out-Null",
            "$owner.Close()",
        ]
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    powershell_exe = str(powershell) if powershell.exists() else "powershell.exe"
    try:
        subprocess.Popen(
            [
                powershell_exe,
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=False,
        )
    except Exception as error:
        print(f"Popup notification failed: {error}", file=sys.stderr)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "JkkHouseWatcher/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_text(DASHBOARD_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/check":
            self._handle_api_check()
            return
        if parsed.path == "/api/stats":
            self._handle_api_stats()
            return
        if parsed.path == "/api/export/listings":
            self._handle_export_json("listings.json")
            return
        if parsed.path == "/api/export/events":
            self._handle_export_json("listing_events.json")
            return
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {format % args}")

    def _handle_api_check(self) -> None:
        try:
            report = build_check_report(
                argparse.Namespace(
                    timeout=self.server.timeout_seconds,  # type: ignore[attr-defined]
                    verify_ssl=self.server.verify_ssl,  # type: ignore[attr-defined]
                    target=self.server.targets,  # type: ignore[attr-defined]
                    exclude=self.server.excludes,  # type: ignore[attr-defined]
                    name_kana=self.server.name_kana,  # type: ignore[attr-defined]
                    no_detail_years=self.server.no_detail_years,  # type: ignore[attr-defined]
                    newer_than_year=self.server.newer_than_year,  # type: ignore[attr-defined]
                    no_ur=self.server.no_ur,  # type: ignore[attr-defined]
                    ur_target=self.server.ur_targets,  # type: ignore[attr-defined]
                    ur_url=self.server.ur_url,  # type: ignore[attr-defined]
                    rules_file=self.server.rules_file,  # type: ignore[attr-defined]
                    data_dir=self.server.data_dir,  # type: ignore[attr-defined]
                    rules=self.server.rules,  # type: ignore[attr-defined]
                )
            )
            update_stats(report, self.server.stats_file, self.server.stats_bucket_minutes)  # type: ignore[attr-defined]
            self._send_json(report_to_dict(report))
        except Exception as error:
            self._send_json({"error": str(error)}, status=500)

    def _handle_api_stats(self) -> None:
        stats = load_stats(self.server.stats_file, self.server.stats_bucket_minutes)  # type: ignore[attr-defined]
        self._send_json(summarize_stats(stats))

    def _handle_export_json(self, filename: str) -> None:
        path = Path(self.server.data_dir) / "data" / filename  # type: ignore[attr-defined]
        if not path.exists():
            self._send_json({} if filename == "listings.json" else [])
            return
        try:
            self._send_json(json.loads(path.read_text(encoding="utf-8")))
        except Exception as error:
            self._send_json({"error": str(error)}, status=500)

    def _send_text(self, body: str, content_type: str, status: int = 200) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], args: argparse.Namespace) -> None:
        super().__init__(server_address, DashboardHandler)
        self.targets = args.target
        self.excludes = args.exclude
        self.name_kana = args.name_kana
        self.timeout_seconds = args.timeout
        self.verify_ssl = args.verify_ssl
        self.no_detail_years = args.no_detail_years
        self.newer_than_year = args.newer_than_year
        self.no_ur = args.no_ur
        self.ur_targets = args.ur_target
        self.ur_url = args.ur_url
        self.rules_file = args.rules_file
        self.data_dir = args.data_dir
        self.rules = args.rules
        self.stats_file = args.stats_file
        self.stats_bucket_minutes = args.stats_bucket_minutes


def run_server(args: argparse.Namespace) -> int:
    server = DashboardServer((args.host, args.port), args)
    url = f"http://{args.host}:{args.port}/"
    print(f"Dashboard: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local watcher for JKK Tokyo ward-area vacancy results."
    )
    parser.add_argument("--target", action="append", default=[], help="Target housing name to highlight. Can be repeated.")
    parser.add_argument("--exclude", action="append", default=[], help="Housing name to hide/mark as excluded. Can be repeated.")
    parser.add_argument("--ur-target", action="append", default=[], help="UR housing name to alert when vacant. Can be repeated.")
    parser.add_argument("--ur-url", default=UR_RESULT_URL, help="UR Kita-ku result URL used as referer/source.")
    parser.add_argument("--no-ur", action="store_true", help="Do not check UR Kita-ku listings.")
    parser.add_argument("--rules-file", default=str(DEFAULT_RULES_FILE), help="JSON rules/config file for lifecycle and refresh behavior.")
    parser.add_argument("--data-dir", default=str(DEFAULT_ALERT_DIR), help="Base directory for lifecycle data, snapshots, and evidence.")
    parser.add_argument("--name-kana", default="", help="Optional value for JKK's 住宅名(カナ) search box. Default: blank.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify JKK SSL certificates. Default is off for this old site.")
    parser.add_argument("--newer-than-year", type=int, default=DEFAULT_NEWER_THAN_YEAR, help="Also alert when a listing's detail page shows a building year at or after this year.")
    parser.add_argument("--no-detail-years", action="store_true", help="Do not open detail pages to read building years.")
    parser.add_argument("--include-excluded", action="store_true", help="Show excluded rows in console output.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text summary.")
    parser.add_argument("--output-html", default="", help="Write a standalone HTML report.")
    parser.add_argument("--beep", action="store_true", help="Play a local alert sound when a target is found.")
    parser.add_argument("--popup", action="store_true", help="Show a silent Windows popup when a target is found.")
    parser.add_argument("--test-popup", action="store_true", help="Show a silent test popup and exit.")
    parser.add_argument("--alert-dir", default=str(DEFAULT_ALERT_DIR), help="Directory for silent watch files.")
    parser.add_argument("--fail-when-missing", action="store_true", help="Exit with code 2 when no target is found.")
    parser.add_argument("--watch", action="store_true", help="Keep checking in a loop.")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Watch interval in seconds.")
    parser.add_argument("--adaptive", action="store_true", help="Use 5-minute checks during historical hot time buckets.")
    parser.add_argument("--fast-interval", type=int, default=DEFAULT_FAST_INTERVAL_SECONDS, help="Adaptive hot-bucket interval in seconds.")
    parser.add_argument("--stats-file", default=str(DEFAULT_STATS_FILE), help="JSON file used to accumulate time-bucket stats.")
    parser.add_argument("--stats-bucket-minutes", type=int, default=DEFAULT_STATS_BUCKET_MINUTES, help="Stats bucket size in minutes.")
    parser.add_argument("--stats", action="store_true", help="Print accumulated adaptive-refresh stats and exit.")
    parser.add_argument("--server", action="store_true", help="Run a local dashboard server.")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard host.")
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.target:
        args.target = DEFAULT_TARGETS[:]
    if not args.exclude:
        args.exclude = DEFAULT_EXCLUDES[:]
    if not args.ur_target:
        args.ur_target = DEFAULT_UR_TARGETS[:]
    args.rules = load_watch_rules(args.rules_file)
    if args.interval == DEFAULT_INTERVAL_SECONDS:
        args.interval = int(args.rules.get("normal_interval_seconds", args.interval))
    if args.fast_interval == DEFAULT_FAST_INTERVAL_SECONDS:
        args.fast_interval = int(args.rules.get("hot_interval_seconds", args.fast_interval))

    if args.stats:
        print(format_stats(load_stats(args.stats_file, args.stats_bucket_minutes)))
        return 0

    if args.test_popup:
        report = CheckReport(
            checked_at=datetime.now().isoformat(timespec="seconds"),
            total_count=1,
            target_names=args.target,
            exclude_names=args.exclude,
            target_found=True,
            visible_count=1,
            excluded_count=0,
            listings=[
                Listing(
                    name="弹窗测试：コーシャハイム加賀",
                    area="テスト区",
                    priority="一般",
                    housing_type="テスト",
                    layout="2LDK",
                    floor_area_m2="50.00",
                    rent_yen="123,000",
                    common_fee_yen="0",
                    units="1",
                    is_target=True,
                )
            ],
        )
        popup(report)
        print("popup-test-triggered")
        return 0

    if args.server:
        return run_server(args)
    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
