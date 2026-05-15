# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time generator for per-region pixel-space assets.

Coastlines and polar projection lookups don't change on human time
scales, so we compute them ONCE on a developer machine and ship the
results. The render path then loads:

* ``_coastline_masks.npz`` — boolean masks at each region's pixel
  dimensions. Overlay = ``rgb[mask] = color`` (numpy fancy index).
* ``_polar_lookups.npz`` — (lat_row, lon_col, valid) per polar
  region. Reindexing a global RGB source into a polar disc is
  ``rgb_polar = rgb_source[lat_row, lon_col]``.

No cartopy at runtime, no per-frame projection.

Run::

    python -m aiseed_weather.figures._precompute_coastlines
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

# Output square for polar hemisphere views. Large enough to read the
# Arctic basin at synoptic scale, small enough that the reindex stays
# inside a few MB of int32 lookup tables.
_POLAR_OUT_SIZE = 800
# Polar disc reaches down to 30°N (or up to 30°S for the Antarctic
# preset). 60° co-latitude → comfortable view of the mid-latitudes
# without showing the equatorial belt where stereographic distortion
# becomes objectionable.
_POLAR_BOUNDARY_COLAT_DEG = 60.0


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
        # width=2 — a 1 px coastline disappears on a regional chart at
        # display sizes typical for the UI (240-pixel-wide previews
        # downscaled further). Two pixels is the minimum that reads as
        # a definite geographic boundary on top of the data overlay
        # without becoming a heavy frame.
        draw.line(list(zip(xs.tolist(), ys.tolist())), fill=255, width=2)
    return np.asarray(img, dtype=bool)


def _polar_lookup(
    is_north: bool, out_size: int = _POLAR_OUT_SIZE,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute (lat_row, lon_col, valid) for one polar hemisphere.

    Equidistant-azimuthal projection: image radius proportional to
    co-latitude from the pole, image angle = longitude. The output
    square has the pole at its centre and the boundary latitude
    (90 - _POLAR_BOUNDARY_COLAT_DEG) at the inscribed circle.

    ``lat_row`` / ``lon_col`` index into the ECMWF Open Data 0.25°
    source grid (721 × 1440), so reindexing a global RGB at render
    time is one numpy fancy index.
    """
    h = w = out_size
    cx = cy = (out_size - 1) / 2.0
    radius = (out_size - 1) / 2.0

    y_idx, x_idx = np.indices((h, w), dtype=np.float32)
    dx = x_idx - cx
    dy = y_idx - cy
    r = np.hypot(dx, dy) / radius
    valid = r <= 1.0

    co_lat = r * _POLAR_BOUNDARY_COLAT_DEG  # 0..60 from the pole
    if is_north:
        lat = 90.0 - co_lat
        # 0° lon at the top of the image, increasing clockwise (east
        # to the right). atan2(dx, -dy) gives that orientation because
        # image y grows downward.
        lon_rad = np.arctan2(dx, -dy)
    else:
        lat = -90.0 + co_lat
        # Mirror so 0° lon is still at top of image but +90° E lies to
        # the left, matching how a south-polar view is usually drawn
        # (looking down through Antarctica).
        lon_rad = np.arctan2(dx, dy)
    lon_deg = np.degrees(lon_rad)

    lat_row = np.clip(
        np.round((90.0 - lat) / _GRID_DEG), 0, _GLOBAL_DIMS[0] - 1,
    ).astype(np.int32)
    lon_col = (
        np.round((lon_deg % 360.0) / _GRID_DEG).astype(np.int32)
        % _GLOBAL_DIMS[1]
    )
    # Out-of-disc pixels still need valid index values for numpy
    # fancy-indexing; the valid mask is what makes them grey later.
    lat_row[~valid] = 0
    lon_col[~valid] = 0
    return lat_row, lon_col, valid


def _polar_project_polylines(
    polylines: list[np.ndarray],
    is_north: bool,
    out_size: int = _POLAR_OUT_SIZE,
) -> tuple[int, int, list[list[tuple[float, float]]]]:
    """Forward-project polyline (lon, lat) vertices to polar pixels.

    Returns (h, w, projected) where ``projected`` is a list of polyline
    segments, each a list of (x, y) image-pixel tuples. Vertices
    outside the hemisphere are split into separate segments so the
    rasteriser doesn't draw straight chords across the disc.
    """
    h = w = out_size
    cx = cy = (out_size - 1) / 2.0
    radius = (out_size - 1) / 2.0

    out: list[list[tuple[float, float]]] = []
    for poly in polylines:
        if poly.shape[0] < 2:
            continue
        lons = poly[:, 0]
        lats = poly[:, 1]
        if is_north:
            co_lat = 90.0 - lats
            in_disc = co_lat <= _POLAR_BOUNDARY_COLAT_DEG
            lon_rad = np.deg2rad(lons)
            r_norm = co_lat / _POLAR_BOUNDARY_COLAT_DEG
            x = cx + r_norm * radius * np.sin(lon_rad)
            y = cy - r_norm * radius * np.cos(lon_rad)
        else:
            co_lat = lats - (-90.0)
            in_disc = co_lat <= _POLAR_BOUNDARY_COLAT_DEG
            lon_rad = np.deg2rad(lons)
            r_norm = co_lat / _POLAR_BOUNDARY_COLAT_DEG
            x = cx + r_norm * radius * np.sin(lon_rad)
            y = cy + r_norm * radius * np.cos(lon_rad)

        # Split into contiguous runs of in-disc vertices.
        segment: list[tuple[float, float]] = []
        for j in range(len(in_disc)):
            if in_disc[j]:
                segment.append((float(x[j]), float(y[j])))
            else:
                if len(segment) >= 2:
                    out.append(segment)
                segment = []
        if len(segment) >= 2:
            out.append(segment)
    return h, w, out


def _rasterise_polar_mask(
    polylines: list[np.ndarray], is_north: bool,
) -> np.ndarray:
    h, w, segments = _polar_project_polylines(polylines, is_north)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for seg in segments:
        draw.line(seg, fill=255, width=1)
    return np.asarray(img, dtype=bool)


def _extract_land_polygons(resolution: str = "110m"):
    """Yield Natural Earth land polygons as (lon, lat) vertex arrays.

    Used to rasterise filled land masks per region, the base layer of
    every chart. Coastlines (a thin polyline drawn on top) are NOT
    derived from these — they come from the dedicated coastline
    shapefile via :func:`_extract_lonlat_polylines`.
    """
    from cartopy.io.shapereader import Reader, natural_earth

    shp_path = natural_earth(
        resolution=resolution, category="physical", name="land",
    )
    for geom in Reader(shp_path).geometries():
        kind = geom.geom_type
        if kind == "Polygon":
            yield np.asarray(geom.exterior.coords, dtype=np.float32)[:, :2]
        elif kind == "MultiPolygon":
            for poly in geom.geoms:
                yield np.asarray(poly.exterior.coords, dtype=np.float32)[:, :2]


def _rasterise_land_mask(
    polygons: list[np.ndarray],
    extent: tuple[float, float, float, float],
    h: int,
    w: int,
) -> np.ndarray:
    """Fill land polygons to a boolean mask (True = land)."""
    lon_min, lon_max, lat_min, lat_max = extent
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    sx = w / (lon_max - lon_min)
    sy = h / (lat_max - lat_min)
    for poly in polygons:
        if poly.shape[0] < 3:
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
        draw.polygon(
            list(zip(xs.tolist(), ys.tolist())), fill=255, outline=None,
        )
    return np.asarray(img, dtype=bool)


def _polar_project_polygons(
    polygons: list[np.ndarray], is_north: bool,
    out_size: int = _POLAR_OUT_SIZE,
) -> tuple[int, int, list[list[tuple[float, float]]]]:
    """Forward-project land polygon vertices to polar pixels."""
    h = w = out_size
    cx = cy = (out_size - 1) / 2.0
    radius = (out_size - 1) / 2.0
    out: list[list[tuple[float, float]]] = []
    for poly in polygons:
        if poly.shape[0] < 3:
            continue
        lons = poly[:, 0]
        lats = poly[:, 1]
        if is_north:
            co_lat = 90.0 - lats
            lon_rad = np.deg2rad(lons)
            r_norm = co_lat / _POLAR_BOUNDARY_COLAT_DEG
            x = cx + r_norm * radius * np.sin(lon_rad)
            y = cy - r_norm * radius * np.cos(lon_rad)
            in_disc = co_lat <= _POLAR_BOUNDARY_COLAT_DEG
        else:
            co_lat = lats - (-90.0)
            lon_rad = np.deg2rad(lons)
            r_norm = co_lat / _POLAR_BOUNDARY_COLAT_DEG
            x = cx + r_norm * radius * np.sin(lon_rad)
            y = cy + r_norm * radius * np.cos(lon_rad)
            in_disc = co_lat <= _POLAR_BOUNDARY_COLAT_DEG
        # For filled polygons we still ship the full polygon; the
        # in-disc mask just confirms the shape touches the hemisphere
        # before paying to project it.
        if not bool(in_disc.any()):
            continue
        seg = [(float(x[j]), float(y[j])) for j in range(len(in_disc))]
        out.append(seg)
    return h, w, out


def _rasterise_polar_land_mask(
    polygons: list[np.ndarray], is_north: bool,
) -> np.ndarray:
    h, w, polys = _polar_project_polygons(polygons, is_north)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    cx = cy = (h - 1) / 2.0
    radius = (h - 1) / 2.0
    for poly in polys:
        if len(poly) >= 3:
            draw.polygon(poly, fill=255, outline=None)
    # Clip to the disc — projected polygons may spill across the disc
    # boundary as straight chords; we don't want those bleed regions
    # contributing to "land".
    yy, xx = np.indices((h, w), dtype=np.float32)
    inside_disc = np.hypot(xx - cx, yy - cy) <= radius
    mask = np.asarray(img, dtype=bool) & inside_disc
    return mask


def build(out_dir: Path | None = None, resolution: str = "110m") -> Path:
    from aiseed_weather.figures.regions import PRESETS

    out_dir = out_dir if out_dir is not None else Path(__file__).parent
    masks_path = out_dir / "_coastline_masks.npz"
    polar_path = out_dir / "_polar_lookups.npz"

    polylines = list(_extract_lonlat_polylines(resolution=resolution))
    print(
        f"Extracted {len(polylines)} polylines "
        f"({sum(len(p) for p in polylines)} vertices) at {resolution}",
    )
    land_polygons = list(_extract_land_polygons(resolution=resolution))
    print(f"Extracted {len(land_polygons)} land polygons")

    masks: dict[str, np.ndarray] = {}
    polar_arrays: dict[str, np.ndarray] = {}

    for region in PRESETS:
        if region.projection in ("north_polar", "south_polar"):
            is_north = region.projection == "north_polar"
            lat_row, lon_col, valid = _polar_lookup(is_north)
            polar_arrays[f"{region.key}__lat_row"] = lat_row
            polar_arrays[f"{region.key}__lon_col"] = lon_col
            polar_arrays[f"{region.key}__valid"] = valid
            mask = _rasterise_polar_mask(polylines, is_north)
            land = _rasterise_polar_land_mask(land_polygons, is_north)
            masks[region.key] = mask
            masks[f"{region.key}__land"] = land
            print(
                f"  region={region.key:14s}  polar  shape={mask.shape}  "
                f"coast={int(mask.sum()):>6d}  land={int(land.sum()):>7d}",
            )
            continue
        h, w = _region_dims(region.extent)
        extent = (
            (-180.0, 180.0, -90.0, 90.0)
            if region.extent is None
            else region.extent
        )
        mask = _rasterise_mask(polylines, extent, h, w)
        land = _rasterise_land_mask(land_polygons, extent, h, w)
        masks[region.key] = mask
        masks[f"{region.key}__land"] = land
        print(
            f"  region={region.key:14s}  flat   shape={(h, w)}  "
            f"coast={int(mask.sum()):>6d}  land={int(land.sum()):>7d}",
        )

    np.savez_compressed(masks_path, **masks)
    print(f"Wrote {masks_path} ({masks_path.stat().st_size} bytes)")
    if polar_arrays:
        np.savez_compressed(polar_path, **polar_arrays)
        print(f"Wrote {polar_path} ({polar_path.stat().st_size} bytes)")
    return masks_path


if __name__ == "__main__":
    build()
