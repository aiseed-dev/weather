# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA AMeDAS (ground observations) service.

Fetches the latest snapshot of all ~1,300 stations across Japan.
Implements the fetch-on-user-action pattern with a 10-minute cache
window so that re-opening the overview view doesn't re-hit JMA.

AMeDAS map JSON keys are station IDs (zero-padded strings). Each
value is an object with sub-arrays per variable, where each sub-array
is ``[value, quality_flag]``. ``quality_flag == 0`` means "good"; any
other flag (1, 2, 8, ...) means the value should be discarded.
Variable presence depends on station type — always check before
reading.

The station table uses ``[deg, min]`` lat/lon pairs (sexagesimal);
we convert to decimal degrees at parse time so downstream code can
stay in WGS-84.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

from aiseed_weather.services import jma_endpoints

logger = logging.getLogger(__name__)


CACHE_WINDOW_SECONDS = 10 * 60
STATION_TABLE_CACHE_DAYS = 7

# Variables we surface to the UI. AMeDAS publishes many more
# (sunshine, humidity, snow depth, etc.) but the overview card only
# wants the headline observations. Keys map AMeDAS' JSON variable
# names → the canonical short names we expose.
_OBS_VARS: dict[str, str] = {
    "temp": "temp",                # air temperature (°C)
    "humidity": "humidity",        # relative humidity (%)
    "precipitation10m": "prcp_10m",
    "precipitation1h": "prcp_1h",
    "precipitation24h": "prcp_24h",
    "wind": "wind_speed",          # wind speed (m/s)
    "windDirection": "wind_dir",   # 16-point compass index (0..16)
    "sun10m": "sun_10m",
    "snow": "snow_depth",
}


@dataclass(frozen=True)
class AmedasStation:
    station_id: str
    name_kana: str
    name_kanji: str
    latitude: float
    longitude: float
    elevation_m: float
    station_type: str  # 'A' (full), 'B' (no temp), 'C' (rainfall only), ...


@dataclass(frozen=True)
class AmedasSnapshot:
    timestamp: datetime
    observations: dict[str, dict[str, float]]
    fetched_at: datetime


class JmaAmedasService:
    """AMeDAS fetcher with disk-cached station table + map snapshot."""

    def __init__(self, *, data_dir: Path):
        self._cache_dir = data_dir / "jma" / "amedas"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._latest_path = self._cache_dir / "_latest_snapshot.json"
        self._station_table_path = self._cache_dir / "amedastable.json"
        # Lock around the network calls so two concurrent overview
        # renders don't double-fetch the same minute's data.
        self._fetch_lock = asyncio.Lock()

    # ── public API ───────────────────────────────────────────────

    async def fetch(self, *, force: bool = False) -> AmedasSnapshot:
        if not force and self._cache_is_fresh():
            try:
                return self._load_from_cache()
            except (OSError, json.JSONDecodeError, KeyError):
                logger.exception(
                    "Cached AMeDAS snapshot unreadable; re-downloading",
                )
        async with self._fetch_lock:
            if not force and self._cache_is_fresh():
                return self._load_from_cache()
            return await self._download_and_cache()

    async def stations(self) -> dict[str, AmedasStation]:
        if self._station_table_is_fresh():
            try:
                raw = json.loads(self._station_table_path.read_text("utf-8"))
                return self._parse_station_table(raw)
            except (OSError, json.JSONDecodeError):
                logger.exception(
                    "Cached station table unreadable; re-downloading",
                )
        return await self._download_station_table()

    # ── cache helpers ────────────────────────────────────────────

    def _cache_is_fresh(self) -> bool:
        if not self._latest_path.exists():
            return False
        age = time.time() - self._latest_path.stat().st_mtime
        return age < CACHE_WINDOW_SECONDS

    def _station_table_is_fresh(self) -> bool:
        if not self._station_table_path.exists():
            return False
        age_days = (
            time.time() - self._station_table_path.stat().st_mtime
        ) / 86400
        return age_days < STATION_TABLE_CACHE_DAYS

    def _load_from_cache(self) -> AmedasSnapshot:
        raw = json.loads(self._latest_path.read_text("utf-8"))
        return AmedasSnapshot(
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            observations={
                sid: {k: float(v) for k, v in obs.items()}
                for sid, obs in raw["observations"].items()
            },
            fetched_at=datetime.fromisoformat(raw["fetched_at"]),
        )

    def _save_to_cache(self, snapshot: AmedasSnapshot) -> None:
        self._latest_path.write_text(
            json.dumps(
                {
                    "timestamp": snapshot.timestamp.isoformat(),
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "observations": snapshot.observations,
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )

    # ── network ──────────────────────────────────────────────────

    async def _download_and_cache(self) -> AmedasSnapshot:
        headers = {"User-Agent": jma_endpoints.USER_AGENT}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            ts_resp = await client.get(jma_endpoints.AMEDAS_LATEST_TIME)
            ts_resp.raise_for_status()
            latest_iso = ts_resp.text.strip()
            # 'latest_time.txt' is ISO with JST offset
            # (e.g. '2026-05-17T14:00:00+09:00'). The map snapshot URL
            # wants compact form '20260517140000' so reformat.
            latest_dt = datetime.fromisoformat(latest_iso)
            stamp = latest_dt.strftime("%Y%m%d%H%M%S")
            snap_url = jma_endpoints.AMEDAS_MAP_SNAPSHOT.format(
                timestamp=stamp,
            )
            logger.info("Fetching AMeDAS snapshot for %s", latest_iso)
            snap_resp = await client.get(snap_url)
            snap_resp.raise_for_status()
            raw = snap_resp.json()

        observations: dict[str, dict[str, float]] = {}
        for sid, station_obs in raw.items():
            kept: dict[str, float] = {}
            for jma_key, our_key in _OBS_VARS.items():
                pair = station_obs.get(jma_key)
                # AMeDAS values are [value, quality_flag]; flag 0 is OK,
                # any other value means the reading is suspect/missing.
                if (
                    not isinstance(pair, list)
                    or len(pair) < 2
                    or pair[1] != 0
                    or pair[0] is None
                ):
                    continue
                try:
                    kept[our_key] = float(pair[0])
                except (TypeError, ValueError):
                    continue
            if kept:
                observations[sid] = kept

        snapshot = AmedasSnapshot(
            timestamp=latest_dt,
            observations=observations,
            fetched_at=datetime.now(latest_dt.tzinfo or None),
        )
        try:
            self._save_to_cache(snapshot)
        except OSError:
            logger.exception("Could not persist AMeDAS snapshot cache")
        return snapshot

    async def _download_station_table(self) -> dict[str, AmedasStation]:
        headers = {"User-Agent": jma_endpoints.USER_AGENT}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.get(jma_endpoints.AMEDAS_STATION_TABLE)
            resp.raise_for_status()
            raw = resp.json()
        try:
            self._station_table_path.write_text(
                json.dumps(raw, ensure_ascii=False), "utf-8",
            )
        except OSError:
            logger.exception("Could not persist AMeDAS station table cache")
        return self._parse_station_table(raw)

    # ── parsing ──────────────────────────────────────────────────

    def _parse_station_table(
        self, raw: dict[str, Any],
    ) -> dict[str, AmedasStation]:
        out: dict[str, AmedasStation] = {}
        for sid, info in raw.items():
            try:
                lat = _decimal_degrees(info.get("lat"))
                lon = _decimal_degrees(info.get("lon"))
            except (TypeError, ValueError):
                continue
            if lat is None or lon is None:
                continue
            try:
                elev = float(info.get("alt") or 0.0)
            except (TypeError, ValueError):
                elev = 0.0
            out[str(sid)] = AmedasStation(
                station_id=str(sid),
                name_kana=str(info.get("knName") or ""),
                name_kanji=str(info.get("kjName") or ""),
                latitude=lat,
                longitude=lon,
                elevation_m=elev,
                station_type=str(info.get("type") or ""),
            )
        return out


# ── module-level helpers ─────────────────────────────────────────


def _decimal_degrees(value: Any) -> float | None:
    """Convert a JMA ``[deg, min]`` pair (or a single decimal float)
    into decimal degrees. Returns ``None`` if the input is missing or
    unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and len(value) >= 2:
        try:
            deg, minutes = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        return deg + minutes / 60.0
    return None


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Great-circle distance in kilometres. Mean Earth radius
    (6371 km) is accurate to ~0.3 %, fine for picking nearest
    AMeDAS stations among options that are tens of km apart."""
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def nearest_stations(
    stations: dict[str, AmedasStation],
    latitude: float,
    longitude: float,
    *,
    limit: int = 3,
    types: Iterable[str] | None = None,
) -> list[tuple[AmedasStation, float]]:
    """Return up to ``limit`` nearest AMeDAS stations to (lat, lon),
    as ``(station, distance_km)`` tuples sorted ascending. ``types``
    optionally filters to specific station-type codes (e.g. ``('A',)``
    for full-instrument stations only).
    """
    type_set = set(types) if types is not None else None
    scored: list[tuple[AmedasStation, float]] = []
    for st in stations.values():
        if type_set is not None and st.station_type not in type_set:
            continue
        d = haversine_km(latitude, longitude, st.latitude, st.longitude)
        scored.append((st, d))
    scored.sort(key=lambda pair: pair[1])
    return scored[:limit]
