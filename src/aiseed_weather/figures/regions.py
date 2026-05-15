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


GLOBAL = Region("global", "全球 / Global", "platecarree", None)
ARCTIC = Region(
    "arctic", "北極中心 / Arctic",
    projection="north_polar",
    # Equidistant-azimuthal disc from the pole out to 30° N. The
    # extent is informational only — the renderer uses a precomputed
    # polar reindex table, not a plate-carrée crop.
    extent=(-180.0, 180.0, 30.0, 90.0),
)
ANTARCTIC = Region(
    "antarctic", "南極中心 / Antarctic",
    projection="south_polar",
    extent=(-180.0, 180.0, -90.0, -30.0),
)
# Matches JMA ASAS (Asian Surface Analysis) standard chart extent —
# 100°E to 180°E, equator to 60°N. The narrower zoom that just frames
# the Japanese archipelago lost the synoptic context (Siberian high,
# Pacific high, mid-latitude lows tracking off the continent) that
# every JP analyst expects on a "日本周辺" chart.
JAPAN = Region("japan", "日本周辺 / Japan", "platecarree", (100.0, 180.0, 0.0, 60.0))
EAST_ASIA = Region("east_asia", "東アジア / East Asia", "platecarree", (95.0, 165.0, 5.0, 60.0))
NORTH_PACIFIC = Region(
    "north_pacific", "北太平洋 / N. Pacific", "platecarree", (130.0, 240.0, 10.0, 65.0),
)
NORTH_ATLANTIC = Region(
    "north_atlantic", "北大西洋 / N. Atlantic", "platecarree", (-80.0, 20.0, 20.0, 70.0),
)
EUROPE = Region("europe", "ヨーロッパ / Europe", "platecarree", (-25.0, 50.0, 30.0, 72.0))
NORTH_AMERICA = Region(
    "north_america", "北米 / N. America", "platecarree", (-170.0, -50.0, 15.0, 75.0),
)


PRESETS: tuple[Region, ...] = (
    GLOBAL,
    ARCTIC,
    ANTARCTIC,
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
