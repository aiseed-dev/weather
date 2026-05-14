# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA rainfall nowcast (radar) service.

Fetches tile-based radar composites from JMA. Implements the
fetch-on-user-action pattern: nothing happens until the view calls fetch().
Cache window is 10 minutes (JMA radar update cadence).

Implementation note for the agent:
This module is a skeleton. The radar product is delivered as XYZ map tiles,
not a single image. A complete implementation will:

1. Call RADAR_TARGET_TIMES to discover the latest basetime/validtime
2. Determine which tiles are needed for the requested viewport
3. Download tiles in parallel (cap at 4 concurrent)
4. Composite tiles into a single image array
5. Return the array plus metadata (validtime, source attribution)

Always check the JMA targetTimes response for the actual URL pattern before
hardcoding. JMA has changed tile paths between versions.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from aiseed_weather.services import jma_endpoints


CACHE_WINDOW_SECONDS = 10 * 60  # JMA radar updates every ~5 min; 10 min is comfortable


@dataclass(frozen=True)
class RadarSnapshot:
    basetime: str           # the time of the radar observation
    validtime: str          # the valid time of the rendered product
    tiles: dict[tuple[int, int, int], bytes]  # (z, x, y) -> PNG bytes
    fetched_at: datetime


class JmaRadarService:
    def __init__(self, *, data_dir: Path):
        self._cache_dir = data_dir / "jma" / "radar"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._cache_dir / "_latest_meta.json"

    async def fetch(self, *, force: bool = False) -> RadarSnapshot:
        """Return the latest radar snapshot.

        With force=False (default), returns the cached snapshot if it is
        younger than CACHE_WINDOW_SECONDS. With force=True, always re-fetches.
        """
        if not force and self._cache_is_fresh():
            return self._load_from_cache()
        return await self._download_and_cache()

    def _cache_is_fresh(self) -> bool:
        if not self._meta_path.exists():
            return False
        age = time.time() - self._meta_path.stat().st_mtime
        return age < CACHE_WINDOW_SECONDS

    def _load_from_cache(self) -> RadarSnapshot:
        # TODO(agent): read meta + tile files from disk into a RadarSnapshot
        raise NotImplementedError

    async def _download_and_cache(self) -> RadarSnapshot:
        # TODO(agent): implementation outline:
        # 1. async with httpx.AsyncClient(headers={"User-Agent": jma_endpoints.USER_AGENT}):
        # 2. GET jma_endpoints.RADAR_TARGET_TIMES → parse JSON, pick latest entry
        # 3. Determine required (z, x, y) tiles for the viewport
        # 4. Fetch tiles concurrently with asyncio.Semaphore(4)
        # 5. Save tiles to self._cache_dir/<basetime>/<validtime>/<z>/<x>/<y>.png
        # 6. Write meta JSON to self._meta_path
        # 7. Return RadarSnapshot
        raise NotImplementedError
