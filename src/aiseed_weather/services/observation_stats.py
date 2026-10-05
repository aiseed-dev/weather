# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Statistics over JMA daily records (``observation_archive.DailyBlock``).

The rules match the 個人開発気象統計 site, which was checked against the
old WeatherCore pages and against JMA practice:

* 資料不足 values (quality ≤ 4) are listed, marked, in a single day's
  ranking but never enter a count, an extreme, a mean or a total.
* Ties share a rank (1, 2, 2, 4 …). Within a tie, stations are ordered
  north to south by forecast-district number, then station code.
* When an extreme occurs on several days, the latest day is reported.
* A station enters a period statistic only when it has values on at
  least ``min_cover`` of the days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from aiseed_weather.services.observation_archive import DailyBlock, ObsStation

ELEMENT_INFO: dict[str, tuple[str, str]] = {
    "tmax": ("日最高気温", "°C"),
    "tmin": ("日最低気温", "°C"),
    "tavg": ("日平均気温", "°C"),
    "precip": ("日降水量", "mm"),
    "sun": ("日照時間", "h"),
}

# Island and summit stations that the site keeps out of "how hot was Japan
# today" lists; offered as an option rather than forced here.
REMOTE_STATIONS = frozenset({47991, 47639})     # 南鳥島, 富士山


@dataclass(frozen=True)
class Condition:
    element: str
    op: str             # ">=" or "<"
    threshold: float

    def test(self, a: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return a >= self.threshold if self.op == ">=" else a < self.threshold

    def label(self) -> str:
        name, unit = ELEMENT_INFO[self.element]
        return f"{name} {'≥' if self.op == '>=' else '<'} {self.threshold:g} {unit}"


@dataclass(frozen=True)
class RankedRow:
    rank: int
    code: int
    value: float
    short: bool = False         # 資料不足
    on: date | None = None      # day of the extreme, when the value is one


def _order_keys(codes: np.ndarray, stations: dict[int, ObsStation]) -> np.ndarray:
    """Position of each station in north-to-south order (tie-breaker)."""
    keys = [(stations[c].prec_no if c in stations else 99, int(c)) for c in codes]
    pos = np.empty(len(keys), dtype=np.int64)
    pos[sorted(range(len(keys)), key=keys.__getitem__)] = np.arange(len(keys))
    return pos


def rank(indices: np.ndarray, values: np.ndarray, pos: np.ndarray, *,
         descending: bool, decimals: int = 1) -> list[tuple[int, int]]:
    """[(rank, index)] sorted by value (rounded for display), then north to south."""
    shown = np.round(values, decimals)
    sign = -1.0 if descending else 1.0
    order = sorted(range(len(indices)), key=lambda n: (sign * shown[n], pos[indices[n]]))
    out, prev, r = [], None, 0
    for i, n in enumerate(order, 1):
        if shown[n] != prev:
            r, prev = i, shown[n]
        out.append((r, n))
    return out


def _mask_excluded(block: DailyBlock, exclude: frozenset[int]) -> np.ndarray:
    return np.isin(block.codes, list(exclude)) if exclude else np.zeros(len(block.codes), bool)


def day_ranking(block: DailyBlock, stations: dict[int, ObsStation], d: date,
                cond: Condition, *,
                exclude: frozenset[int] = frozenset()) -> list[RankedRow] | None:
    """Stations meeting ``cond`` on ``d``, highest value first for ≥ and lowest first for <.

    None when no station has a value that day (no record, not "nobody").
    """
    j = block.day_index(d)
    if not 0 <= j < block.n_days:
        return None
    col = block.vals[cond.element][:, j].astype(np.float64)
    col[_mask_excluded(block, exclude)] = np.nan
    if np.isnan(col).all():
        return None
    hit = np.nonzero(cond.test(col))[0]
    pos = _order_keys(block.codes, stations)
    short = block.short[cond.element][:, j]
    return [RankedRow(rank=r, code=int(block.codes[hit[n]]), value=float(col[hit[n]]),
                      short=bool(short[hit[n]]))
            for r, n in rank(hit, col[hit], pos, descending=cond.op == ">=")]


@dataclass(frozen=True)
class PeriodRow:
    code: int
    days_with_data: int
    count: int | None           # days meeting the condition (None without a condition)
    max: float | None
    max_on: date | None
    min: float | None
    min_on: date | None
    mean: float | None
    total: float | None         # precipitation and sunshine only


def period_summary(block: DailyBlock, element: str, cond: Condition | None, *,
                   min_cover: float = 0.8,
                   exclude: frozenset[int] = frozenset()) -> list[PeriodRow]:
    """Per-station statistics of ``element`` over the whole block.

    ``cond`` (on any element) adds a count of qualifying days. 資料不足
    values are left out of everything; stations below ``min_cover`` are
    left out entirely.
    """
    a = block.vals[element].astype(np.float64)
    a[block.short[element]] = np.nan
    have = ~np.isnan(a)
    n_have = have.sum(axis=1)
    keep = (n_have >= min_cover * block.n_days) & ~_mask_excluded(block, exclude)
    if cond is not None:
        c = block.vals[cond.element].astype(np.float64)
        c[block.short[cond.element]] = np.nan
        counts = cond.test(c).sum(axis=1)
    rows = []
    summable = element in ("precip", "sun")
    for i in np.nonzero(keep)[0]:
        x = a[i]
        ok = have[i]
        # latest day of the extreme: search the reversed series
        jmax = len(x) - 1 - int(np.nanargmax(x[::-1]))
        jmin = len(x) - 1 - int(np.nanargmin(x[::-1]))
        rows.append(PeriodRow(
            code=int(block.codes[i]), days_with_data=int(n_have[i]),
            count=int(counts[i]) if cond is not None else None,
            max=float(x[jmax]), max_on=block.date_at(jmax),
            min=float(x[jmin]), min_on=block.date_at(jmin),
            mean=float(x[ok].mean()),
            total=float(x[ok].sum()) if summable else None,
        ))
    return rows


PERIOD_METRICS: dict[str, str] = {
    "count": "条件を満たした日数",
    "max": "期間の最高",
    "min": "期間の最低",
    "mean": "期間の平均",
    "total": "期間の合計",
}


def rank_period(rows: list[PeriodRow], stations: dict[int, ObsStation], metric: str, *,
                descending: bool, top: int | None = None) -> list[tuple[int, PeriodRow]]:
    """[(rank, row)] by ``metric``; rows without that metric are dropped."""
    usable = [r for r in rows if getattr(r, metric) is not None]
    if metric == "count":
        usable = [r for r in usable if r.count > 0]
    if not usable:
        return []
    codes = np.array([r.code for r in usable])
    pos = _order_keys(codes, stations)
    vals = np.array([float(getattr(r, metric)) for r in usable])
    ranked = rank(np.arange(len(usable)), vals, pos, descending=descending,
                  decimals=0 if metric == "count" else 1)
    out = [(r, usable[n]) for r, n in ranked]
    return [x for x in out if top is None or x[0] <= top]
