# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA daily observation records (1880–), read from the redistributed NetCDF.

Two kinds of file, both listed in ``manifest.json``:

* ``years/{year}.nc`` — every station for one calendar year. All year
  files carry the same 1,342 stations in the same order, so a period
  that spans years is a concatenation along the day axis.
* ``stations/{code}.nc`` — one station, its whole record.

Caching follows the publisher's cadence. The manifest is re-read at
most hourly (the site rebuilds hourly). A cached year file is reused
while its sha256 matches the manifest — past years therefore never
re-download unless the publisher corrects them; the current year
re-downloads when a new day lands. Station files carry no hash in the
manifest, so their byte size stands in for it (the time axis grows
daily, which changes the size).

Values follow JMA's quality codes: 8 normal, 5 準正常, 4 資料不足 and
below unusable for statistics. ``DailyBlock.short`` marks the 資料不足
cells; ``observation_stats`` keeps them out of means, extremes and day
counts but shows them (marked) in a single day's list, which is how
JMA itself treats them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from aiseed_weather.services import observation_endpoints as ep

logger = logging.getLogger(__name__)

MANIFEST_WINDOW_SECONDS = 60 * 60
EPOCH = date(1870, 1, 1)                     # "days since 1870-01-01" in the files
ELEMENTS = ("tmax", "tmin", "tavg", "precip", "sun")

Progress = Callable[[str], None]


@dataclass(frozen=True)
class ObsStation:
    code: int
    name: str
    kana: str
    pref: str
    prec_no: int
    lat: float
    lon: float
    alt: float
    active: bool
    first: date | None
    last: date | None


@dataclass(frozen=True)
class Manifest:
    coverage_first: date
    coverage_last: date
    attribution: str
    license: str
    years: dict[int, tuple[str, str, int]]       # year → (path, sha256, bytes)
    stations: dict[int, tuple[str, int]]         # code → (path, bytes)
    fetched_at: datetime


@dataclass(frozen=True)
class DailyBlock:
    """All stations × consecutive days. Values in physical units, NaN = missing."""

    codes: np.ndarray                    # (n_station,) int
    first: date
    vals: dict[str, np.ndarray]          # element → (n_station, n_day) float
    short: dict[str, np.ndarray]         # element → (n_station, n_day) bool, 資料不足

    @property
    def n_days(self) -> int:
        return next(iter(self.vals.values())).shape[1]

    def day_index(self, d: date) -> int:
        return (d - self.first).days

    def date_at(self, j: int) -> date:
        return self.first + timedelta(days=int(j))


@dataclass(frozen=True)
class StationSeries:
    code: int
    first: date
    vals: dict[str, np.ndarray]          # element → (n_day,) float
    short: dict[str, np.ndarray]         # element → (n_day,) bool


class ObservationArchive:
    def __init__(self, *, data_dir: Path):
        self._dir = data_dir / "observations" / "daily"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._dir / "manifest.json"
        self._stations_path = self._dir / "stations.json"
        self._lock = asyncio.Lock()

    # ── manifest and station list ───────────────────────────────

    async def manifest(self, *, force: bool = False) -> Manifest:
        async with self._lock:
            fresh = (self._manifest_path.exists() and
                     time.time() - self._manifest_path.stat().st_mtime < MANIFEST_WINDOW_SECONDS)
            if force or not fresh:
                await self._download(ep.DAILY_MANIFEST, self._manifest_path)
                await self._download(ep.DAILY_FILE.format(path="stations.json"),
                                     self._stations_path)
            return _parse_manifest(json.loads(self._manifest_path.read_text("utf-8")),
                                   datetime.fromtimestamp(self._manifest_path.stat().st_mtime))

    async def stations(self, *, force: bool = False) -> dict[int, ObsStation]:
        await self.manifest(force=force)
        raw = json.loads(self._stations_path.read_text("utf-8"))
        return {int(s["code"]): _parse_station(s) for s in raw["stations"]}

    # ── data files ───────────────────────────────────────────────

    async def year_path(self, year: int, manifest: Manifest, *, force: bool = False) -> Path | None:
        """The cached year file, downloaded if missing or outdated. None if not published."""
        entry = manifest.years.get(year)
        if entry is None:
            return None
        rel, sha, _ = entry
        dst = self._dir / rel
        if not force and dst.exists() and await asyncio.to_thread(_sha256, dst) == sha:
            return dst
        await self._download(ep.DAILY_FILE.format(path=rel), dst)
        got = await asyncio.to_thread(_sha256, dst)
        if got != sha:
            # The site rebuilt between reading the manifest and the file.
            # The file is newer than the manifest, not corrupt; keep it.
            logger.warning("Year %d: sha256 differs from manifest (site rebuilt meanwhile)", year)
        return dst

    async def station_path(self, code: int, manifest: Manifest, *,
                           force: bool = False) -> Path | None:
        entry = manifest.stations.get(code)
        if entry is None:
            return None
        rel, size = entry
        dst = self._dir / rel
        if not force and dst.exists() and dst.stat().st_size == size:
            return dst
        await self._download(ep.DAILY_FILE.format(path=rel), dst)
        return dst

    async def load_period(self, start: date, end: date, *, elements: tuple[str, ...] = ELEMENTS,
                          force: bool = False,
                          progress: Progress | None = None) -> tuple[DailyBlock, Manifest]:
        """Every station for start..end (inclusive), clipped to the published coverage.

        Only ``elements`` are read: a 20-year block of all stations is ~40 MB
        per element in float32, so callers ask for what they use."""
        say = progress or (lambda _m: None)
        say("Reading the archive manifest…")
        m = await self.manifest(force=force)
        if start > m.coverage_last or end < m.coverage_first:
            raise ValueError(f"{start}〜{end} の記録はまだありません（公開されているのは "
                             f"{m.coverage_first}〜{m.coverage_last}。"
                             "前日分は毎日 2 時ごろに加わります）")
        start = max(start, m.coverage_first)
        end = min(end, m.coverage_last)
        paths = []
        years = range(start.year, end.year + 1)
        for i, y in enumerate(years, 1):
            say(f"Fetching daily records · {y} ({i}/{len(years)})…")
            p = await self.year_path(y, m, force=force)
            if p is None:
                raise ValueError(f"{y} is not in the archive")
            paths.append(p)
        say("Reading NetCDF…")
        block = await asyncio.to_thread(_read_years, paths, start, end, elements)
        return block, m

    async def load_station(self, code: int, *, force: bool = False,
                           progress: Progress | None = None) -> tuple[StationSeries, Manifest]:
        say = progress or (lambda _m: None)
        say("Reading the archive manifest…")
        m = await self.manifest(force=force)
        say(f"Fetching the record of station {code}…")
        p = await self.station_path(code, m, force=force)
        if p is None:
            raise ValueError(f"Station {code} is not in the archive")
        say("Reading NetCDF…")
        return await asyncio.to_thread(_read_station, p, code), m

    # ── network ──────────────────────────────────────────────────

    async def _download(self, url: str, dst: Path) -> None:
        logger.info("Downloading %s", url)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        headers = {"User-Agent": ep.USER_AGENT}
        async with (
            httpx.AsyncClient(timeout=60.0, headers=headers, follow_redirects=True) as client,
            client.stream("GET", url) as r,
        ):
            r.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        tmp.replace(dst)


# ── parsing and reading (sync; called through asyncio.to_thread) ──


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _date_or_none(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _parse_manifest(raw: dict, fetched_at: datetime) -> Manifest:
    first, last = raw["coverage"].split("..")
    return Manifest(
        coverage_first=date.fromisoformat(first),
        coverage_last=date.fromisoformat(last),
        attribution=raw.get("attribution") or ep.ATTRIBUTION,
        license=raw.get("license", ""),
        years={int(e["year"]): (e["path"], e["sha256"], int(e["bytes"])) for e in raw["years"]},
        stations={int(e["code"]): (e["path"], int(e["bytes"])) for e in raw["stations"]},
        fetched_at=fetched_at,
    )


def _parse_station(s: dict) -> ObsStation:
    return ObsStation(
        code=int(s["code"]), name=s.get("name") or str(s["code"]), kana=s.get("kana") or "",
        pref=s.get("pref") or "", prec_no=int(s.get("prec_no") or 99),
        lat=float(s.get("lat") or 0.0), lon=float(s.get("lon") or 0.0),
        alt=float(s.get("alt") or 0.0), active=bool(s.get("active")),
        first=_date_or_none(s.get("first")), last=_date_or_none(s.get("last")),
    )


def _read_vars(ds, sl, elements: tuple[str, ...] = ELEMENTS
               ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    vals, short = {}, {}
    for v in elements:
        a = np.ma.filled(ds[v][sl].astype(np.float32), np.nan)
        if v == "precip" and "precip_none" in ds.variables:
            # JMA writes "--" (no precipitation phenomenon) rather than 0.0
            none = np.ma.filled(ds["precip_none"][sl], -1) == 1
            a[np.isnan(a) & none] = 0.0
        vals[v] = a
        if f"{v}_q" in ds.variables:
            q = np.ma.filled(ds[f"{v}_q"][sl].astype(np.int16), -1)
            short[v] = (q >= 0) & (q <= 4)
        else:
            short[v] = np.zeros(a.shape, dtype=bool)
    return vals, short


def _read_years(paths: list[Path], start: date, end: date,
                elements: tuple[str, ...] = ELEMENTS) -> DailyBlock:
    import netCDF4

    codes = None
    parts_v: dict[str, list] = {v: [] for v in elements}
    parts_s: dict[str, list] = {v: [] for v in elements}
    for p in paths:
        with netCDF4.Dataset(p) as ds:
            c = np.asarray(ds["station_code"][:])
            if codes is None:
                codes = c
            elif not np.array_equal(codes, c):
                raise ValueError(f"{p.name}: station order differs from the other years")
            first = EPOCH + timedelta(days=int(ds["time"][0]))
            n = ds.dimensions["time"].size
            j0 = max(0, (start - first).days)
            j1 = min(n, (end - first).days + 1)
            vals, short = _read_vars(ds, (slice(None), slice(j0, j1)), elements)
            for v in elements:
                parts_v[v].append(vals[v])
                parts_s[v].append(short[v])
    return DailyBlock(
        codes=codes,
        first=start,
        vals={v: np.concatenate(parts_v[v], axis=1) for v in elements},
        short={v: np.concatenate(parts_s[v], axis=1) for v in elements},
    )


def _read_station(path: Path, code: int) -> StationSeries:
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        first = EPOCH + timedelta(days=int(ds["time"][0]))
        vals, short = _read_vars(ds, (0, slice(None)))
    return StationSeries(code=code, first=first, vals=vals, short=short)
