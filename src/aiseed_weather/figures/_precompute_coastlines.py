# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time generator for per-region coastline masks.

Coastlines don't change, so we run cartopy + Natural Earth ONCE on a
development machine and ship the result. The render path then loads
boolean masks at the exact pixel dimensions every layer uses and
overlays them in-place with ``rgb[mask] = color`` — no per-frame
projection, no per-frame line drawing.

Run::

    python -m aiseed_weather.figures._precompute_coastlines

This writes ``_coastline_masks.npz`` next to this file: one boolean
mask per region preset, keyed by ``region.key``. The file is checked
into git so end users never need cartopy installed for rendering.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# 0.25° matches ECMWF Open Data (the source the renderers consume).
# Both data and mask are sampled at exactly the same grid, so the
# in-place ``rgb[mask] = color`` overlay just works without any
# rescaling.
_GRID_DEG = 0.25
_GLOBAL_DIMS = (721, 1440)  # (height, width) lat-major


def _extract_lonlat_polylines(resolution: str = "110m"):
    from cartopy.io.shapereader import Reader, natural_earth

    shp_path = natural_earth(
        resolution=resolution, category="physical", name="coastline",
    )
    reader = Reader(shp_path)
    for geom in reader.geometries():
        kind = geom.geom_type
        if kind == "LineString":
            yield np.asarray(geom.coords, dtype=np.float32)[:, :2]
        elif kind == "MultiLineString":
            for sub in geom.geoms:
                yield np.asarray(sub.coords, dtype=np.float32)[:, :2]
        elif kind == "Polygon":
            yield np.asarray(geom.exterior.coords, dtype=np.float32)[:, :2]
            for ring in geom.interiors:
                yield np.asarray(ring.coords, dtype=np.float32)[:, :2]
        elif kind == "MultiPolygon":
            for poly in geom.geoms:
                yield np.asarray(poly.exterior.coords, dtype=np.float32)[:, :2]
                for ring in poly.interiors:
                    yield np.asarray(ring.coords, dtype=np.float32)[:, :2]


def _region_dims(extent: tuple[float, float, float, float] | None) -> tuple[int, int]:
    """(H, W) pixels for a region at the ECMWF 0.25° grid."""
    if extent is None:
        return _GLOBAL_DIMS
    lon_min, lon_max, lat_min, lat_max = extent
    w = int(round((lon_max - lon_min) / _GRID_DEG)) + 1
    h = int(round((lat_max - lat_min) / _GRID_DEG)) + 1
    return h, w


def _rasterise_mask(
    polylines: list[np.ndarray],
    extent: tuple[float, float, float, float],
    h: int,
    w: int,
) -> np.ndarray:
    """Draw the polylines onto a 1-channel image, then convert to bool.

    Image coords: x = (lon - lon_min) / (lon_max - lon_min) * w
                  y = (lat_max - lat) / (lat_max - lat_min) * h
    (Lat decreases downward because image rows go top→bottom.)
    """
    lon_min, lon_max, lat_min, lat_max = extent
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    sx = w / (lon_max - lon_min)
    sy = h / (lat_max - lat_min)
    for poly in polylines:
        if poly.shape[0] < 2:
            continue
        lons = poly[:, 0]
        lats = poly[:, 1]
        if (
            lons.max() < lon_min or lons.min() > lon_max
            or lats.max() < lat_min or lats.min() > lat_max
        ):
            continue
        xs = (lons - lon_min) * sx
        ys = (lat_max - lats) * sy
        draw.line(list(zip(xs.tolist(), ys.tolist())), fill=255, width=1)
    return np.asarray(img, dtype=bool)


def build(out_path: Path | None = None, resolution: str = "110m") -> Path:
    from aiseed_weather.figures.regions import PRESETS

    out_path = (
        out_path if out_path is not None
        else Path(__file__).parent / "_coastline_masks.npz"
    )
    polylines = list(_extract_lonlat_polylines(resolution=resolution))
    print(
        f"Extracted {len(polylines)} polylines "
        f"({sum(len(p) for p in polylines)} vertices) at {resolution}",
    )

    masks: dict[str, np.ndarray] = {}
    for region in PRESETS:
        h, w = _region_dims(region.extent)
        extent = (
            (-180.0, 180.0, -90.0, 90.0)
            if region.extent is None
            else region.extent
        )
        mask = _rasterise_mask(polylines, extent, h, w)
        masks[region.key] = mask
        on = int(mask.sum())
        print(
            f"  region={region.key:14s}  shape={(h, w)}  "
            f"coast_pixels={on:>6d}",
        )

    np.savez_compressed(out_path, **masks)
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


if __name__ == "__main__":
    build()
