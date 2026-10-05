# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time generator for the AMeDAS map's coastline and land.

The 0.25° masks in ``_coastline_masks.npz`` match the ECMWF grid but are
far coarser than AMeDAS station spacing (~17 km), so the station map
gets its own outline of the archipelago: Natural Earth 10m coastline and
land, clipped to ``amedas_map.EXTENT`` and simplified to ~500 m. The
result is a few hundred kB of (lon, lat) vertices in
``_japan_coastline.npz``; the render path draws them with matplotlib and
never imports cartopy.

Run (downloads Natural Earth into cartopy's data directory once)::

    python -m aiseed_weather.figures._precompute_japan_coast
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

SIMPLIFY_DEG = 0.005
OUT = Path(__file__).parent / "_japan_coastline.npz"


def _lines(geom):
    if geom.is_empty:
        return
    kind = geom.geom_type
    if kind == "LineString":
        yield np.asarray(geom.coords, dtype=np.float32)[:, :2]
    elif kind == "Polygon":
        yield np.asarray(geom.exterior.coords, dtype=np.float32)[:, :2]
    elif hasattr(geom, "geoms"):
        for sub in geom.geoms:
            yield from _lines(sub)


def _join(parts: list[np.ndarray]) -> np.ndarray:
    """Concatenate polylines with a NaN row between them (matplotlib breaks there)."""
    sep = np.full((1, 2), np.nan, dtype=np.float32)
    out = []
    for p in parts:
        if len(p) >= 2:
            out += [p, sep]
    return np.concatenate(out) if out else np.empty((0, 2), np.float32)


def build() -> Path:
    from cartopy.io.shapereader import Reader, natural_earth
    from shapely.geometry import box

    from aiseed_weather.figures.amedas_map import EXTENT

    lon0, lon1, lat0, lat1 = EXTENT
    frame = box(lon0 - 0.5, lat0 - 0.5, lon1 + 0.5, lat1 + 0.5)
    result = {}
    for name in ("coastline", "land"):
        shp = natural_earth(resolution="10m", category="physical", name=name)
        parts = []
        for geom in Reader(shp).geometries():
            if not geom.intersects(frame):
                continue
            clipped = geom.intersection(frame).simplify(SIMPLIFY_DEG, preserve_topology=True)
            parts += list(_lines(clipped))
        result[name] = _join(parts)
        print(f"{name}: {len(parts)} parts, {len(result[name])} vertices")
    np.savez_compressed(OUT, coast=result["coastline"], land=result["land"],
                        extent=np.array(EXTENT, dtype=np.float32),
                        source=np.array("Natural Earth 10m (public domain)"))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return OUT


if __name__ == "__main__":
    build()
