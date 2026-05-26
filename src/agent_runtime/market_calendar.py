"""거래일 판정 유틸. KR은 KIS chk-holiday API, US는 pandas_market_calendars(NYSE)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

_KST = timezone(timedelta(hours=9))
_KR_HOLIDAY_MAX_PAGES = 5

_KR_HOLIDAY_CACHE: dict[str, dict[str, bool]] = {}  # month_key("YYYYMM") -> { "YYYYMMDD": is_open }
_KR_LOCK = threading.Lock()
_US_CALENDAR: Any | None = None


def _today_kst() -> date:
    return datetime.now(tz=_KST).date()


def _kis_client() -> Any:
    from alpha_engine.kis.simpleki import SimpleKI
    from alpha_engine.settings import settings

    return SimpleKI(settings.KIS_KEYFILE_PATH)


def _extract_continuation(payload: dict[str, Any]) -> tuple[str, str]:
    return (
        str(payload.get("ctx_area_fk") or payload.get("CTX_AREA_FK") or "").strip(),
        str(payload.get("ctx_area_nk") or payload.get("CTX_AREA_NK") or "").strip(),
    )


def _fetch_kr_holiday_month(month_key: str) -> dict[str, bool]:
    """KIS chk-holiday(CTCA0903R)를 직접 호출.

    KIS는 한 달 데이터도 24행 단위로 pagination 할 수 있다. 첫 페이지만 읽으면
    25일 이후 휴장일을 캐시하지 못하고 평일 fallback으로 오판할 수 있다.
    """
    client = _kis_client()
    bass_dt = f"{month_key}01"
    url = f"{client.URL_BASE}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {client.ACCESS_TOKEN}",
        "appKey": client.APP_KEY,
        "appSecret": client.APP_SECRET,
        "custtype": "P",
        "tr_id": "CTCA0903R",
    }
    params = {"BASS_DT": bass_dt, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
    result: dict[str, bool] = {}

    for _ in range(_KR_HOLIDAY_MAX_PAGES):
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        payload = res.json()
        rows = payload.get("output", []) or []

        for row in rows:
            bass = row.get("bass_dt") or row.get("BASS_DT")
            opnd_yn = (row.get("opnd_yn") or row.get("OPND_YN") or "").upper()
            if not bass:
                continue
            if str(bass).startswith(month_key):
                result[str(bass)] = opnd_yn == "Y"

        tr_cont = str(res.headers.get("tr_cont") or "").strip()
        if tr_cont not in {"M", "F"}:
            break

        row_dates = [str(row.get("bass_dt") or row.get("BASS_DT") or "") for row in rows]
        if result and row_dates and all(row_date[:6] > month_key for row_date in row_dates if row_date):
            break

        ctx_area_fk, ctx_area_nk = _extract_continuation(payload)
        if not ctx_area_fk and not ctx_area_nk:
            break
        headers["tr_cont"] = "N"
        params["CTX_AREA_FK"] = ctx_area_fk
        params["CTX_AREA_NK"] = ctx_area_nk

    return result


def _ensure_kr_month(month_key: str) -> dict[str, bool]:
    with _KR_LOCK:
        cached = _KR_HOLIDAY_CACHE.get(month_key)
        if cached is not None:
            return cached
        fetched = _fetch_kr_holiday_month(month_key)
        _KR_HOLIDAY_CACHE[month_key] = fetched
        return fetched


def is_kr_trading_day(target: date) -> bool:
    month_key = target.strftime("%Y%m")
    day_key = target.strftime("%Y%m%d")
    month = _ensure_kr_month(month_key)
    if day_key in month:
        return month[day_key]
    # KIS가 한 달 데이터를 끊어 줄 수 있으니 캐시 미스 시 주말 fallback
    return target.weekday() < 5


def last_kr_trading_day(before_or_on: date) -> date:
    cursor = before_or_on
    for _ in range(14):
        if is_kr_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return before_or_on


def _us_calendar() -> Any:
    global _US_CALENDAR
    if _US_CALENDAR is None:
        import pandas_market_calendars as mcal

        _US_CALENDAR = mcal.get_calendar("NYSE")
    return _US_CALENDAR


def is_us_trading_day(target: date) -> bool:
    cal = _us_calendar()
    schedule = cal.schedule(start_date=target.isoformat(), end_date=target.isoformat())
    return len(schedule) > 0


def last_us_trading_day(before_or_on: date) -> date:
    cal = _us_calendar()
    start = (before_or_on - timedelta(days=14)).isoformat()
    schedule = cal.schedule(start_date=start, end_date=before_or_on.isoformat())
    if len(schedule) == 0:
        return before_or_on
    return schedule.index[-1].date()


@dataclass
class CalendarSnapshot:
    date: str
    weekday: str
    kr_open_today: bool
    us_open_today: bool
    kr_last_trading_day: str
    us_last_trading_day: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weekday": self.weekday,
            "kr_open_today": self.kr_open_today,
            "us_open_today": self.us_open_today,
            "kr_last_trading_day": self.kr_last_trading_day,
            "us_last_trading_day": self.us_last_trading_day,
        }


def build_snapshot(target: date | None = None) -> CalendarSnapshot:
    today = target or _today_kst()
    kr_open = is_kr_trading_day(today)
    us_open = is_us_trading_day(today)
    kr_last = today if kr_open else last_kr_trading_day(today - timedelta(days=1))
    us_last = today if us_open else last_us_trading_day(today - timedelta(days=1))
    return CalendarSnapshot(
        date=today.isoformat(),
        weekday=today.strftime("%A"),
        kr_open_today=kr_open,
        us_open_today=us_open,
        kr_last_trading_day=kr_last.isoformat(),
        us_last_trading_day=us_last.isoformat(),
    )
