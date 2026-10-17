# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Collapse a day of hourly WMO weather codes into one headline code.

Taking the max code of the day makes a single hour of 0.1 mm drizzle the
whole day's weather, because WMO numbers 51+ are all precipitation. The
rules here follow how JMA words a day instead: the daytime sky decides,
and rain or snow only wins when the day's wet hours add up to 1 mm.

Models scatter light precipitation over Japan that is mostly never
observed: AMeDAS rain gauges resolve 0.5 mm, so an hour below that counts
as dry everywhere in this module.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

_DAY_START_HOUR = 6
_DAY_END_HOUR = 18
_PRECIP_DAY_MM = 1.0
WET_HOUR_MM = 0.5

_DRIZZLE = frozenset({51, 53, 55})
_FREEZING = frozenset({56, 57, 66, 67})
# An hour that reaches the wet threshold is rain, not drizzle.
_RAIN = frozenset({61, 63, 65, 80, 81, 82}) | _DRIZZLE
_SNOW = frozenset({71, 73, 75, 77, 85, 86})
# Listed in tie-break order: snow beats rain beats freezing.
_PRECIP_GROUPS = (_SNOW, _RAIN, _FREEZING)
_PRECIP_CODES = _DRIZZLE | _FREEZING | _RAIN | _SNOW

_FOG = frozenset({45, 48})
_FOG_SKY_LEVEL = -1
# Used when the model reports >= 1 mm without any precipitation code.
_INCONSISTENT_PRECIP_CODE = 61


def _is_wet(precip_mm: float | None) -> bool:
    return precip_mm is not None and precip_mm >= WET_HOUR_MM


def hourly_display_code(
    code: int | None, precip_mm: float | None,
) -> int | None:
    """Code to draw for one hour: a precipitation code whose hour stays
    under the wet threshold (or has no amount) is shown as 曇り."""
    if code is not None and int(code) in _PRECIP_CODES and not _is_wet(precip_mm):
        return 3
    return code


def daily_weather_code(
    local_hours: Sequence[int],
    codes: Sequence[int | None],
    precip_mm: Sequence[float | None],
) -> int | None:
    """One WMO code for the day, or None when no hour has a code.

    ``local_hours`` is the location's clock hour (0..23) of each row after
    the end-of-hour shift, so the three sequences line up row by row.
    """
    rows = [
        (int(h), int(c), p)
        for h, c, p in zip(local_hours, codes, precip_mm)
        if c is not None
    ]
    if not rows:
        return None

    daytime = [
        (h, c, p) for h, c, p in rows
        if _DAY_START_HOUR <= h < _DAY_END_HOUR
    ]
    # A partial day (e.g. from this evening on) has no 06-18 rows; judge
    # by what exists instead of reporting nothing.
    if not daytime:
        daytime = rows

    thunder = [c for _, c, _ in daytime if c >= 95]
    if thunder:
        return max(thunder)

    wet_rows = [(c, p) for _, c, p in rows if _is_wet(p)]
    if sum(p for _, p in wet_rows) >= _PRECIP_DAY_MM:
        return _precipitation_code([c for c, _ in wet_rows])

    return _sky_code([c for _, c, _ in daytime])


def _precipitation_code(all_codes: list[int]) -> int:
    counts = Counter(c for c in all_codes if c in _PRECIP_CODES)
    if not counts:
        return _INCONSISTENT_PRECIP_CODE
    best_hours = -1
    best_group: frozenset[int] = _RAIN
    for group in _PRECIP_GROUPS:
        hours = sum(n for c, n in counts.items() if c in group)
        # Strict > keeps the earlier (higher-priority) group on a tie.
        if hours > best_hours:
            best_hours, best_group = hours, group
    code = max(c for c in counts if c in best_group)
    # Drizzle that still reaches the wet threshold reads as light rain.
    return _INCONSISTENT_PRECIP_CODE if code in _DRIZZLE else code


def _sky_level(code: int) -> int:
    if code in _FOG:
        return _FOG_SKY_LEVEL
    if code in (0, 1, 2, 3):
        return code
    # Light precipitation that did not add up to a rainy day is
    # reported as the cloud it fell from.
    return 3


def _sky_code(daytime_codes: list[int]) -> int:
    levels = [_sky_level(c) for c in daytime_codes]
    fog_hours = sum(1 for lv in levels if lv == _FOG_SKY_LEVEL)
    if fog_hours * 2 >= len(levels):
        return 45
    clear_levels = sorted(lv for lv in levels if lv != _FOG_SKY_LEVEL)
    return clear_levels[(len(clear_levels) - 1) // 2]
