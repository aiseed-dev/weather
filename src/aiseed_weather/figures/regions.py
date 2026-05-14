# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Named region presets for synoptic chart rendering.

Each region carries a cartopy-style extent (lon_min, lon_max, lat_min,
lat_max in PlateCarree degrees) and a default projection that's sensible
for that area. Render functions in :mod:`aiseed_weather.figures` accept
a :class:`Region` and set the axes extent + projection accordingly.

Longitudes are in [-180, 180] for everywhere except the North Pacific
preset, where we use 0..360 (or rather a domain that crosses the
antimeridian) so the basin isn't split in half on the chart.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    key: str           # stable identifier (e.g. used in state, config)
    label: str         # human-readable display name
    projection: str    # one of msl_chart._PROJECTIONS keys
    # cartopy extent (lon_min, lon_max, lat_min, lat_max) in PlateCarree
    # degrees. None means "global" — render functions call set_global().
    extent: tuple[float, float, float, float] | None


GLOBAL = Region("global", "全球 / Global", "robinson", None)
JAPAN = Region("japan", "日本周辺 / Japan", "mercator", (115.0, 155.0, 20.0, 50.0))
EAST_ASIA = Region("east_asia", "東アジア / East Asia", "mercator", (95.0, 165.0, 5.0, 60.0))
NORTH_PACIFIC = Region(
    "north_pacific", "北太平洋 / N. Pacific", "mercator", (130.0, 240.0, 10.0, 65.0),
)
NORTH_ATLANTIC = Region(
    "north_atlantic", "北大西洋 / N. Atlantic", "mercator", (-80.0, 20.0, 20.0, 70.0),
)
EUROPE = Region("europe", "ヨーロッパ / Europe", "mercator", (-25.0, 50.0, 30.0, 72.0))
NORTH_AMERICA = Region(
    "north_america", "北米 / N. America", "mercator", (-170.0, -50.0, 15.0, 75.0),
)


PRESETS: tuple[Region, ...] = (
    GLOBAL,
    JAPAN,
    EAST_ASIA,
    NORTH_PACIFIC,
    NORTH_ATLANTIC,
    EUROPE,
    NORTH_AMERICA,
)


def custom_region(
    lon_min: float, lon_max: float, lat_min: float, lat_max: float,
) -> Region:
    """Build an ad-hoc Region from user-supplied bounds.

    Always uses the Mercator projection because PlateCarree distortion
    becomes objectionable at small extents. Callers are responsible for
    validating that lat_min < lat_max and lon_min < lon_max (and that
    latitudes stay within Mercator's safe band, roughly ±82°).
    """
    return Region(
        key="custom",
        label=(
            f"任意 / Custom ({lon_min:.0f}, {lon_max:.0f}, "
            f"{lat_min:.0f}, {lat_max:.0f})"
        ),
        projection="mercator",
        extent=(lon_min, lon_max, lat_min, lat_max),
    )


def by_key(key: str) -> Region:
    for r in PRESETS:
        if r.key == key:
            return r
    raise KeyError(f"No region preset with key={key!r}")
