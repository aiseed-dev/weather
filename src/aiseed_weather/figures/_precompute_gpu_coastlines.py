# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time generator for the coastlines the GPU map view draws.

The GPU view (flet-gpufield) draws coastlines as vector polylines on top
of the shaded field, re-projected every frame, so it needs (lon, lat)
vertices rather than the raster masks in ``_coastline_masks.npz``.

Natural Earth (public domain):
  * ``global``     50m coastline, simplified to ~0.03° (global, polar views)
  * ``east_asia``  10m coastline clipped to 90–190°E / 0–66°N, simplified
                   to ~0.01° (regional views around Japan)

Each set is one float32 (N, 2) array of (lon, lat) with NaN rows between
lines. Runtime code never imports cartopy.

Run (downloads Natural Earth into cartopy's data directory once)::

    python -m aiseed_weather.figures._precompute_gpu_coastlines
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "_gpu_coastlines.npz"
EAST_ASIA_BOX = (90.0, -170.0, 0.0, 66.0)  # lon_w, lon_e (across 180), lat_s, lat_n


def _lines(geom):
    if geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield np.asarray(geom.coords, dtype=np.float32)[:, :2]
    elif hasattr(geom, "geoms"):
        for sub in geom.geoms:
            yield from _lines(sub)


def _pack(lines) -> np.ndarray:
    parts = []
    for ln in lines:
        if len(ln) >= 2:
            parts.append(ln)
            parts.append(np.array([[np.nan, np.nan]], dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)


def main() -> None:
    import cartopy.io.shapereader as shpreader
    from shapely.geometry import box

    def read(resolution: str):
        path = shpreader.natural_earth(resolution=resolution, category="physical", name="coastline")
        return [g for g in shpreader.Reader(path).geometries()]

    global_lines = []
    for g in read("50m"):
        for ln in _lines(g.simplify(0.03)):
            global_lines.append(ln)

    east = []
    boxes = [box(90.0, 0.0, 180.0, 66.0), box(-180.0, 0.0, -170.0, 66.0)]
    for g in read("10m"):
        for b in boxes:
            if not g.intersects(b):
                continue
            for ln in _lines(g.intersection(b).simplify(0.01)):
                east.append(ln)

    np.savez_compressed(OUT, **{"global": _pack(global_lines), "east_asia": _pack(east)})
    print(f"wrote {OUT}: global {sum(len(x) for x in global_lines)} pts, "
          f"east_asia {sum(len(x) for x in east)} pts")


if __name__ == "__main__":
    main()
