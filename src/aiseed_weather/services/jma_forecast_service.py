# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA 府県天気予報 fetcher.

The forecast endpoint (``/bosai/forecast/data/forecast/{office}.json``)
returns a list with two ``timeSeries`` publishers:

 1. **Short term** (today / tomorrow / day-after-tomorrow). Each
    sub-element holds weather codes, winds, and waves at a 6-hour
    cadence, plus per-day max-temperature arrays.
 2. **Week-ahead** (today..+7d). Per-day weather codes, max temp,
    min temp, and reliability flags ('A' / 'B' / 'C').

The JMA hierarchy is::

    centers (regional headquarters)
      └── offices (e.g. '130000' Tokyo) ← forecast keyed here
            └── class10 (e.g. '130010' 東京地方) ← timeSeries area key
                  └── class15
                        └── class20 (AMeDAS-level point granularity)

We resolve a (lat, lon) → office_code by looking up the nearest
class10 area (whose representative AMeDAS station has known lat/lon
via the AMeDAS station table) and walking up to its parent office.

Cache window is 1 hour: JMA's official update cadence is 05:00 /
11:00 / 17:00 JST so a short cache window is enough to dedupe a
session's repeated reads without serving anything materially stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from aiseed_weather.services import jma_endpoints
from aiseed_weather.services.jma_amedas_service import (
    AmedasStation, haversine_km,
)

logger = logging.getLogger(__name__)


CACHE_WINDOW_SECONDS = 60 * 60
AREA_TABLE_CACHE_DAYS = 30


@dataclass(frozen=True)
class AreaResolution:
    """Result of mapping (lat, lon) → JMA forecast area.

    ``office_code`` keys the forecast endpoint;
    ``class10_code`` keys the per-area arrays inside the response.
    """
    office_code: str
    office_name: str
    class10_code: str
    class10_name: str
    distance_km: float


@dataclass(frozen=True)
class DailyForecast:
    """One day's headline forecast — short-term days carry weather
    code + min/max temp; week-ahead days additionally carry a
    precipitation probability and a reliability flag."""
    date: date
    weather_code: int | None
    weather_text: str | None
    temp_max: float | None
    temp_min: float | None
    precip_prob_pct: int | None
    reliability: str | None  # 'A' / 'B' / 'C' or None


@dataclass(frozen=True)
class ForecastBundle:
    office_code: str
    office_name: str
    class10_code: str
    class10_name: str
    publishing_office: str
    report_datetime: datetime
    short_term: list[DailyForecast]
    week_ahead: list[DailyForecast]


class JmaForecastService:
    """Forecast fetcher with disk-cached area table + per-office
    forecast JSON. Cache windows: 30 days for the area hierarchy
    (which never changes between releases), 1 hour for forecasts
    (JMA publishes 3× per day).
    """

    def __init__(self, *, data_dir: Path):
        self._cache_dir = data_dir / "jma" / "forecast"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._area_table_path = self._cache_dir / "area.json"
        self._fetch_lock = asyncio.Lock()

    # ── public API ───────────────────────────────────────────────

    async def area_table(self) -> dict[str, Any]:
        if self._area_table_is_fresh():
            try:
                return json.loads(self._area_table_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.exception(
                    "Area table cache unreadable; re-downloading",
                )
        return await self._download_area_table()

    async def resolve_area(
        self,
        latitude: float,
        longitude: float,
        amedas_stations: dict[str, AmedasStation] | None = None,
    ) -> AreaResolution | None:
        """Look up the JMA forecast area that contains (lat, lon).

        Strategy: find the nearest class20 entry whose ID matches an
        AMeDAS station (so we have geographic ground truth), then walk
        up its parent chain → class10 → office. AMeDAS coordinates are
        good enough as a proxy; the alternative (shape-file polygon
        containment) would pull in a heavy geo dependency for marginal
        gain on a coast-vs-inland boundary the user can override.
        """
        if amedas_stations is None:
            return None
        area_table = await self.area_table()
        class20s = area_table.get("class20s") or {}
        class15s = area_table.get("class15s") or {}
        class10s = area_table.get("class10s") or {}
        offices = area_table.get("offices") or {}

        # Score every class20 whose code is also an AMeDAS station id.
        best: tuple[str, float] | None = None
        for code in class20s.keys():
            station = amedas_stations.get(str(code))
            if station is None:
                continue
            d = haversine_km(
                latitude, longitude, station.latitude, station.longitude,
            )
            if best is None or d < best[1]:
                best = (str(code), d)
        if best is None:
            return None

        class20_code, dist_km = best
        c15_code = class20s.get(class20_code, {}).get("parent")
        c10_code = class15s.get(str(c15_code), {}).get("parent") if c15_code else None
        office_code = (
            class10s.get(str(c10_code), {}).get("parent")
            if c10_code else None
        )
        if not (c10_code and office_code):
            return None

        return AreaResolution(
            office_code=str(office_code),
            office_name=str(offices.get(str(office_code), {}).get("name") or ""),
            class10_code=str(c10_code),
            class10_name=str(class10s.get(str(c10_code), {}).get("name") or ""),
            distance_km=dist_km,
        )

    async def fetch(
        self, office_code: str, class10_code: str, *,
        force: bool = False,
    ) -> ForecastBundle:
        cache_path = self._cache_dir / f"forecast_{office_code}.json"
        if not force and self._forecast_cache_is_fresh(cache_path):
            try:
                return self._parse_forecast(
                    json.loads(cache_path.read_text("utf-8")),
                    office_code=office_code, class10_code=class10_code,
                )
            except (OSError, json.JSONDecodeError, KeyError):
                logger.exception(
                    "Cached forecast unreadable; re-downloading",
                )
        async with self._fetch_lock:
            if not force and self._forecast_cache_is_fresh(cache_path):
                return self._parse_forecast(
                    json.loads(cache_path.read_text("utf-8")),
                    office_code=office_code, class10_code=class10_code,
                )
            return await self._download_forecast(
                office_code=office_code, class10_code=class10_code,
                cache_path=cache_path,
            )

    # ── cache helpers ────────────────────────────────────────────

    def _area_table_is_fresh(self) -> bool:
        if not self._area_table_path.exists():
            return False
        age_days = (
            time.time() - self._area_table_path.stat().st_mtime
        ) / 86400
        return age_days < AREA_TABLE_CACHE_DAYS

    def _forecast_cache_is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        return age < CACHE_WINDOW_SECONDS

    # ── network ──────────────────────────────────────────────────

    async def _download_area_table(self) -> dict[str, Any]:
        headers = {"User-Agent": jma_endpoints.USER_AGENT}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.get(jma_endpoints.AREA_TABLE)
            resp.raise_for_status()
            raw = resp.json()
        try:
            self._area_table_path.write_text(
                json.dumps(raw, ensure_ascii=False), "utf-8",
            )
        except OSError:
            logger.exception("Could not persist JMA area table cache")
        return raw

    async def _download_forecast(
        self, *, office_code: str, class10_code: str, cache_path: Path,
    ) -> ForecastBundle:
        url = jma_endpoints.FORECAST_OFFICE.format(office_code=office_code)
        headers = {"User-Agent": jma_endpoints.USER_AGENT}
        logger.info("Fetching JMA forecast for office %s", office_code)
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.json()
        try:
            cache_path.write_text(
                json.dumps(raw, ensure_ascii=False), "utf-8",
            )
        except OSError:
            logger.exception("Could not persist forecast cache: %s", cache_path)
        return self._parse_forecast(
            raw, office_code=office_code, class10_code=class10_code,
        )

    # ── parsing ──────────────────────────────────────────────────

    def _parse_forecast(
        self, raw: list[dict[str, Any]],
        *, office_code: str, class10_code: str,
    ) -> ForecastBundle:
        """Pick the entries for our class10 area out of the two
        publishers in the JMA response. The schema is verbose but
        positional: each ``timeSeries`` row pairs a list of
        ``timeDefines`` (timestamps) with parallel ``areas[].<var>``
        arrays, all in the same JST order.
        """
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            raise ValueError("Empty JMA forecast response")
        head = raw[0]
        publish_office = str(head.get("publishingOffice") or "")
        report_dt = datetime.fromisoformat(head["reportDatetime"])

        short_term = self._extract_short_term(
            head.get("timeSeries") or [], class10_code,
        )
        week_ahead: list[DailyForecast] = []
        if len(raw) > 1:
            week_ahead = self._extract_week_ahead(
                raw[1].get("timeSeries") or [], class10_code,
            )

        return ForecastBundle(
            office_code=office_code,
            office_name="",  # filled by the resolver / caller
            class10_code=class10_code,
            class10_name="",
            publishing_office=publish_office,
            report_datetime=report_dt,
            short_term=short_term,
            week_ahead=week_ahead,
        )

    def _extract_short_term(
        self, time_series: list[dict[str, Any]], class10_code: str,
    ) -> list[DailyForecast]:
        # The first publisher block typically has three rows:
        #  row 0: weather codes (6-hourly cadence, indexed by class10)
        #  row 1: pops (precip-prob % for 0-6/6-12/12-18/18-24)
        #  row 2: per-day temps for AMeDAS-class20 points
        weather_by_day: dict[date, tuple[int | None, str | None]] = {}
        pop_by_day: dict[date, int] = {}

        if time_series:
            row = time_series[0]
            times = [
                datetime.fromisoformat(t)
                for t in row.get("timeDefines", [])
            ]
            area = _find_area(row.get("areas") or [], class10_code)
            if area is not None:
                codes = area.get("weatherCodes") or []
                texts = area.get("weathers") or []
                for i, ts in enumerate(times):
                    day = ts.date()
                    if day in weather_by_day:
                        continue
                    code = _safe_int(codes[i]) if i < len(codes) else None
                    text = (
                        str(texts[i]) if i < len(texts) and texts[i] else None
                    )
                    weather_by_day[day] = (code, text)

        if len(time_series) > 1:
            row = time_series[1]
            times = [
                datetime.fromisoformat(t)
                for t in row.get("timeDefines", [])
            ]
            area = _find_area(row.get("areas") or [], class10_code)
            if area is not None:
                pops = area.get("pops") or []
                for i, ts in enumerate(times):
                    day = ts.date()
                    p = _safe_int(pops[i]) if i < len(pops) else None
                    if p is None:
                        continue
                    # JMA emits per-6h pops; expose the day's max
                    # as the headline figure.
                    pop_by_day[day] = max(pop_by_day.get(day, 0), p)

        # Day temps in short-term are 'today's max / tomorrow's min'
        # bundled inconsistently; we let the week-ahead block carry
        # the canonical min/max numbers instead, leaving short-term
        # to just weather+pop.
        out: list[DailyForecast] = []
        for day in sorted(weather_by_day.keys()):
            code, text = weather_by_day[day]
            out.append(DailyForecast(
                date=day,
                weather_code=code,
                weather_text=text,
                temp_max=None,
                temp_min=None,
                precip_prob_pct=pop_by_day.get(day),
                reliability=None,
            ))
        return out

    def _extract_week_ahead(
        self, time_series: list[dict[str, Any]], class10_code: str,
    ) -> list[DailyForecast]:
        # The second publisher block has two rows:
        #  row 0: per-day weather codes + pops + reliability for the
        #         class10 area
        #  row 1: per-day min/max temps for a representative AMeDAS
        #         station (codes vary by office)
        codes_by_day: dict[date, int | None] = {}
        pops_by_day: dict[date, int | None] = {}
        rel_by_day: dict[date, str | None] = {}
        if time_series:
            row = time_series[0]
            times = [
                datetime.fromisoformat(t)
                for t in row.get("timeDefines", [])
            ]
            area = _find_area(row.get("areas") or [], class10_code)
            if area is not None:
                codes = area.get("weatherCodes") or []
                pops = area.get("pops") or []
                rels = area.get("reliabilities") or []
                for i, ts in enumerate(times):
                    day = ts.date()
                    codes_by_day[day] = (
                        _safe_int(codes[i]) if i < len(codes) else None
                    )
                    pops_by_day[day] = (
                        _safe_int(pops[i]) if i < len(pops) else None
                    )
                    rel_by_day[day] = (
                        str(rels[i]) if i < len(rels) and rels[i] else None
                    )

        tmin_by_day: dict[date, float | None] = {}
        tmax_by_day: dict[date, float | None] = {}
        if len(time_series) > 1:
            row = time_series[1]
            times = [
                datetime.fromisoformat(t)
                for t in row.get("timeDefines", [])
            ]
            for area in row.get("areas") or []:
                tmins = area.get("tempsMin") or []
                tmaxs = area.get("tempsMax") or []
                # First area in the temp row is fine — picking the
                # specific representative AMeDAS would need extra
                # area-code matching that the schema doesn't make
                # easy; the office's primary point is the default.
                for i, ts in enumerate(times):
                    day = ts.date()
                    tmin_by_day.setdefault(
                        day,
                        _safe_float(tmins[i]) if i < len(tmins) else None,
                    )
                    tmax_by_day.setdefault(
                        day,
                        _safe_float(tmaxs[i]) if i < len(tmaxs) else None,
                    )
                break

        days = sorted(set(codes_by_day) | set(tmin_by_day) | set(tmax_by_day))
        out: list[DailyForecast] = []
        for day in days:
            out.append(DailyForecast(
                date=day,
                weather_code=codes_by_day.get(day),
                weather_text=None,
                temp_max=tmax_by_day.get(day),
                temp_min=tmin_by_day.get(day),
                precip_prob_pct=pops_by_day.get(day),
                reliability=rel_by_day.get(day),
            ))
        return out


def _find_area(
    areas: list[dict[str, Any]], class10_code: str,
) -> dict[str, Any] | None:
    for area in areas:
        info = area.get("area") or {}
        if str(info.get("code")) == class10_code:
            return area
    # Some publishers only emit one area for the entire office; fall
    # back to the first one rather than dropping the forecast on the
    # floor.
    return areas[0] if areas else None


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
