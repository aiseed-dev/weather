# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA rainfall nowcast (高解像度降水ナウキャスト) service.

Fetch-on-user-action: nothing happens until the view calls ``times()``
or ``mosaic()``. The time index is reused for 10 minutes (JMA adds an
observation every 5 minutes); the tiles of one (basetime, validtime)
never change, so they stay on disk until they are two hours old.

The product comes as 256 px Web-Mercator tiles drawn in JMA's eight
precipitation-intensity colours (``jma_endpoints``). ``mosaic()`` turns
the tiles covering an area into one array of intensity classes:
0 = no echo, 1 … 8 = JMA's bins (〜1, 1〜5, 5〜10, 10〜20, 20〜30,
30〜50, 50〜80, 80〜 mm/h).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
import numpy as np
from PIL import Image

from aiseed_weather.services import jma_endpoints

logger = logging.getLogger(__name__)

CACHE_WINDOW_SECONDS = 10 * 60
TILE_RETENTION = timedelta(hours=2)
TILE_SIZE = 256
MAX_REQUESTS_PER_SECOND = 4          # jma-data-access etiquette
MAX_CONCURRENT_REQUESTS = 4

# Tile colour of each intensity class 1 … 8 (JMA's legend).
_CLASS_RGB = np.array([
    (242, 242, 255), (160, 210, 255), (33, 140, 255), (0, 65, 255),
    (250, 245, 0), (255, 153, 0), (255, 40, 0), (180, 0, 104),
], dtype=np.int32)
# A tile colour further than this (squared RGB distance) from every legend
# colour means JMA changed its palette; it is still mapped to the nearest.
_UNKNOWN_COLOUR_D2 = 3 * 24 ** 2
_warned_colours: set[int] = set()


def _parse_time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


@dataclass(frozen=True)
class RadarFrame:
    basetime: str           # UTC YYYYmmddHHMMSS: the observation the frame starts from
    validtime: str          # UTC YYYYmmddHHMMSS: the time the frame shows

    @property
    def key(self) -> str:
        return f"{self.basetime}/{self.validtime}"

    @property
    def base(self) -> datetime:
        return _parse_time(self.basetime)

    @property
    def valid(self) -> datetime:
        return _parse_time(self.validtime)

    @property
    def is_forecast(self) -> bool:
        return self.validtime != self.basetime

    @property
    def lead_minutes(self) -> int:
        return int((self.valid - self.base).total_seconds() // 60)


@dataclass(frozen=True)
class RadarTimes:
    frames: tuple[RadarFrame, ...]      # by valid time: observations, then the forecast
    fetched_at: datetime

    @property
    def latest_observation(self) -> RadarFrame:
        return max((f for f in self.frames if not f.is_forecast), key=lambda f: f.validtime)

    def find(self, key: str) -> RadarFrame | None:
        return next((f for f in self.frames if f.key == key), None)


@dataclass(frozen=True, eq=False)
class RadarMosaic:
    """Intensity classes on the Web-Mercator pixel grid of ``zoom``.

    ``classes[r, c]`` is world pixel ``(x0 * 256 + c, y0 * 256 + r)``.
    ``eq=False`` keeps equality by identity (an array has no single truth
    value, and use_state compares old and new values).
    """

    frame: RadarFrame
    zoom: int
    x0: int
    y0: int
    classes: np.ndarray
    tiles: int
    missing: int            # tiles JMA did not deliver (drawn as no echo)
    fetched_at: datetime

    def lons(self) -> np.ndarray:
        """Longitude of each column's pixel centre."""
        px = self.x0 * TILE_SIZE + np.arange(self.classes.shape[1]) + 0.5
        return px / (TILE_SIZE * 2 ** self.zoom) * 360.0 - 180.0

    def lats(self) -> np.ndarray:
        """Latitude of each row's pixel centre (north to south)."""
        py = self.y0 * TILE_SIZE + np.arange(self.classes.shape[0]) + 0.5
        return np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * py / (TILE_SIZE * 2 ** self.zoom)))))


def tile_range(extent: tuple[float, float, float, float], zoom: int) -> tuple[int, int, int, int]:
    """(x0, x1, y0, y1), inclusive, of the tiles covering a lon/lat extent."""
    lon0, lon1, lat0, lat1 = extent
    n = 2 ** zoom

    def x_of(lon: float) -> int:
        return int((lon + 180.0) / 360.0 * n)

    def y_of(lat: float) -> int:
        s = math.sin(math.radians(lat))
        return int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)

    return x_of(lon0), min(x_of(lon1), n - 1), y_of(lat1), min(y_of(lat0), n - 1)


def _classify(colours: np.ndarray) -> np.ndarray:
    """Packed 0xRRGGBB colours → intensity class 1 … 8 (nearest legend colour)."""
    rgb = np.stack([(colours >> 16) & 255, (colours >> 8) & 255, colours & 255], axis=1)
    d2 = ((rgb[:, None, :] - _CLASS_RGB[None, :, :]) ** 2).sum(axis=2)
    nearest = d2.argmin(axis=1)
    for c, d in zip(colours, d2[np.arange(len(colours)), nearest]):
        if d > _UNKNOWN_COLOUR_D2 and int(c) not in _warned_colours:
            _warned_colours.add(int(c))
            logger.warning("JMA radar tile colour #%06x is not in the legend", int(c))
    return (nearest + 1).astype(np.uint8)


def decode_tile(data: bytes) -> np.ndarray:
    """One tile PNG → (256, 256) intensity classes."""
    rgba = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
    if rgba.shape[:2] != (TILE_SIZE, TILE_SIZE):
        raise ValueError(f"JMA radar tile is {rgba.shape[1]}×{rgba.shape[0]}, expected 256×256")
    out = np.zeros(rgba.shape[:2], np.uint8)
    echo = rgba[..., 3] >= 128
    if echo.any():
        rgb = rgba[..., :3].astype(np.int32)
        packed = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
        colours, inverse = np.unique(packed[echo], return_inverse=True)
        out[echo] = _classify(colours)[inverse]
    return out


def _assemble(blobs: dict[tuple[int, int], bytes | None],
              x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
    out = np.zeros(((y1 - y0 + 1) * TILE_SIZE, (x1 - x0 + 1) * TILE_SIZE), np.uint8)
    for (x, y), data in blobs.items():
        if data:
            r, c = (y - y0) * TILE_SIZE, (x - x0) * TILE_SIZE
            out[r:r + TILE_SIZE, c:c + TILE_SIZE] = decode_tile(data)
    return out


class _RequestPacer:
    """Spaces request starts at least 1 / per_second apart."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class JmaRadarService:
    def __init__(self, *, data_dir: Path):
        self._cache_dir = data_dir / "jma" / "radar"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._cache_dir / "_latest_meta.json"
        # One frame at a time, so a quick second request cannot double the
        # rate JMA sees.
        self._tile_lock = asyncio.Lock()

    # ── time index ──────────────────────────────────────────────

    async def times(self, *, force: bool = False) -> RadarTimes:
        """Observations of the past 3 hours and the 1-hour forecast.

        With force=False the index fetched within CACHE_WINDOW_SECONDS is
        reused; force=True always asks JMA.
        """
        if not force and self._meta_is_fresh():
            try:
                return self._times_from(json.loads(self._meta_path.read_text("utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                logger.exception("Cached radar time index unreadable; re-downloading")
        headers = {"User-Agent": jma_endpoints.USER_AGENT}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            observations = await self._get_json(client, jma_endpoints.RADAR_TARGET_TIMES)
            forecasts = await self._get_json(client, jma_endpoints.RADAR_TARGET_TIMES_FORECAST)
        raw = {"fetched_at": datetime.now(UTC).isoformat(),
               "observations": observations, "forecasts": forecasts}
        times = self._times_from(raw)
        try:
            self._meta_path.write_text(json.dumps(raw), "utf-8")
        except OSError:
            logger.exception("Could not persist the radar time index")
        return times

    @staticmethod
    async def _get_json(client: httpx.AsyncClient, url: str):
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    def _meta_is_fresh(self) -> bool:
        if not self._meta_path.exists():
            return False
        return time.time() - self._meta_path.stat().st_mtime < CACHE_WINDOW_SECONDS

    @staticmethod
    def _times_from(raw: dict) -> RadarTimes:
        element = jma_endpoints.RADAR_ELEMENT
        frames: dict[str, RadarFrame] = {}
        for entry in raw["observations"]:
            if element in entry.get("elements", ()) and entry["basetime"] == entry["validtime"]:
                f = RadarFrame(entry["basetime"], entry["validtime"])
                frames[f.key] = f
        for entry in raw["forecasts"]:
            if element in entry.get("elements", ()):
                f = RadarFrame(entry["basetime"], entry["validtime"])
                frames[f.key] = f
        if not any(not f.is_forecast for f in frames.values()):
            raise ValueError(f"JMA's nowcast index lists no {element} observation")
        ordered = tuple(sorted(frames.values(), key=lambda f: (f.validtime, f.basetime)))
        return RadarTimes(frames=ordered, fetched_at=datetime.fromisoformat(raw["fetched_at"]))

    # ── tiles ───────────────────────────────────────────────────

    async def mosaic(self, frame: RadarFrame, extent: tuple[float, float, float, float],
                     zoom: int, *,
                     on_progress: Callable[[int, int], None] | None = None) -> RadarMosaic:
        """Intensity classes of one frame over ``extent`` at ``zoom``.

        Tiles already on disk are not fetched again. ``on_progress(done,
        total)`` is called as tiles arrive.
        """
        if zoom % 2:
            raise ValueError(f"JMA radar tiles carry data only at even zoom levels, not {zoom}")
        async with self._tile_lock:
            await asyncio.to_thread(self._prune)
            x0, x1, y0, y1 = tile_range(extent, zoom)
            coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
            frame_dir = self._cache_dir / frame.basetime / frame.validtime / str(zoom)
            blobs: dict[tuple[int, int], bytes | None] = {}
            todo = []
            for x, y in coords:
                path = frame_dir / str(x) / f"{y}.png"
                if path.exists():
                    blobs[(x, y)] = path.read_bytes()
                else:
                    todo.append((x, y))
            done = len(coords) - len(todo)
            if on_progress:
                on_progress(done, len(coords))
            if todo:
                logger.info("Fetching %d radar tiles for %s (z%d)", len(todo), frame.key, zoom)
                pacer = _RequestPacer(MAX_REQUESTS_PER_SECOND)
                slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
                headers = {"User-Agent": jma_endpoints.USER_AGENT}
                async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:

                    async def fetch_tile(x: int, y: int) -> None:
                        nonlocal done
                        url = jma_endpoints.RADAR_TILE_TEMPLATE.format(
                            basetime=frame.basetime, validtime=frame.validtime,
                            element=jma_endpoints.RADAR_ELEMENT, z=zoom, x=x, y=y,
                        )
                        async with slots:
                            await pacer.wait()
                            try:
                                resp = await client.get(url)
                                resp.raise_for_status()
                            except httpx.HTTPError as exc:
                                logger.warning("Radar tile %s failed: %s", url, exc)
                                blobs[(x, y)] = None
                            else:
                                blobs[(x, y)] = resp.content
                                self._save_tile(frame_dir / str(x) / f"{y}.png", resp.content)
                        done += 1
                        if on_progress:
                            on_progress(done, len(coords))

                    await asyncio.gather(*(fetch_tile(x, y) for x, y in todo))
            missing = sum(1 for v in blobs.values() if v is None)
            if missing == len(coords):
                raise RuntimeError(f"JMA delivered none of the {len(coords)} radar tiles "
                                   f"for {frame.validtime} UTC")
            classes = await asyncio.to_thread(_assemble, blobs, x0, x1, y0, y1)
        return RadarMosaic(frame=frame, zoom=zoom, x0=x0, y0=y0, classes=classes,
                           tiles=len(coords), missing=missing, fetched_at=datetime.now(UTC))

    @staticmethod
    def _save_tile(path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            logger.exception("Could not cache radar tile %s", path)

    def _prune(self) -> None:
        """Delete the tiles of basetimes older than TILE_RETENTION."""
        cutoff = datetime.now(UTC) - TILE_RETENTION
        for d in self._cache_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                stale = _parse_time(d.name) < cutoff
            except ValueError:
                continue
            if stale:
                shutil.rmtree(d, ignore_errors=True)
