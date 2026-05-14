# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA AMeDAS (ground observations) service.

Fetches the latest snapshot of all ~1,300 stations across Japan. Implements
the fetch-on-user-action pattern with a 10-minute cache window.

AMeDAS map JSON keys are station IDs (zero-padded strings). Each value is an
object with sub-arrays per variable, where each sub-array is
[value, quality_flag]. Variable presence depends on station type — always
check before reading.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from aiseed_weather.services import jma_endpoints


CACHE_WINDOW_SECONDS = 10 * 60
STATION_TABLE_CACHE_DAYS = 7


@dataclass(frozen=True)
class AmedasStation:
    station_id: str
    name_kana: str
    name_kanji: str
    latitude: float
    longitude: float
    elevation_m: float
    station_type: str  # full / temperature_only / rainfall_only / etc.


@dataclass(frozen=True)
class AmedasSnapshot:
    timestamp: datetime
    # station_id -> { variable_name -> value }
    # quality flags are filtered out at decode time
    observations: dict[str, dict[str, float]]
    fetched_at: datetime


class JmaAmedasService:
    def __init__(self, *, data_dir: Path):
        self._cache_dir = data_dir / "jma" / "amedas"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._latest_path = self._cache_dir / "_latest_snapshot.json"
        self._station_table_path = self._cache_dir / "amedastable.json"

    async def fetch(self, *, force: bool = False) -> AmedasSnapshot:
        if not force and self._cache_is_fresh():
            return self._load_from_cache()
        return await self._download_and_cache()

    async def stations(self) -> dict[str, AmedasStation]:
        """Return the station table, refreshing once a week."""
        if self._station_table_is_fresh():
            return self._parse_station_table(json.loads(self._station_table_path.read_text("utf-8")))
        return await self._download_station_table()

    def _cache_is_fresh(self) -> bool:
        if not self._latest_path.exists():
            return False
        age = time.time() - self._latest_path.stat().st_mtime
        return age < CACHE_WINDOW_SECONDS

    def _station_table_is_fresh(self) -> bool:
        if not self._station_table_path.exists():
            return False
        age_days = (time.time() - self._station_table_path.stat().st_mtime) / 86400
        return age_days < STATION_TABLE_CACHE_DAYS

    def _load_from_cache(self) -> AmedasSnapshot:
        # TODO(agent): rebuild AmedasSnapshot from self._latest_path JSON
        raise NotImplementedError

    async def _download_and_cache(self) -> AmedasSnapshot:
        # TODO(agent): implementation outline:
        # 1. GET AMEDAS_LATEST_TIME (plain text timestamp like "2026-05-14T03:00:00+09:00")
        # 2. Parse to YYYYMMDDHHMMSS form expected by the map endpoint
        # 3. GET AMEDAS_MAP_SNAPSHOT formatted with that timestamp
        # 4. Decode: each station's variables come as [value, quality_flag];
        #    keep only values where quality_flag indicates good data
        # 5. Write to self._latest_path
        # 6. Return AmedasSnapshot
        raise NotImplementedError

    async def _download_station_table(self) -> dict[str, AmedasStation]:
        # TODO(agent): GET AMEDAS_STATION_TABLE, parse into AmedasStation dataclasses
        raise NotImplementedError

    def _parse_station_table(self, raw: dict[str, Any]) -> dict[str, AmedasStation]:
        # TODO(agent): structure of amedastable.json:
        # { "<station_id>": {"kjName": "...", "knName": "...", "lat": [deg, min], ...} }
        # Convert [deg, min] coordinate pairs to decimal degrees.
        raise NotImplementedError
