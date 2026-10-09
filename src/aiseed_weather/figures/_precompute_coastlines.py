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


_NE_CACHE = Path.home() / ".cache" / "aiseed-weather" / "natural_earth"
_NE_URL = "https://naturalearth.s3.amazonaws.com/{res}_{cat}/ne_{res}_{name}.zip"


def _natural_earth_path(resolution: str, category: str, name: str) -> Path:
    """Shapefile path via cartopy when installed, else a pyshp-readable
    copy fetched once into ~/.cache (the dev venv on this machine has no
    cartopy; the conda env on the server does. Same public-domain data)."""
    try:
        from cartopy.io.shapereader import natural_earth
        return Path(natural_earth(resolution=resolution, category=category, name=name))
    except ImportError:
        pass
    shp = _NE_CACHE / f"ne_{resolution}_{name}.shp"
    if not shp.exists():
        import io
        import urllib.request
        import zipfile
        _NE_CACHE.mkdir(parents=True, exist_ok=True)
        url = _NE_URL.format(res=resolution, cat=category, name=name)
        with urllib.request.urlopen(url, timeout=120) as res:
            zipfile.ZipFile(io.BytesIO(res.read())).extractall(_NE_CACHE)
    return shp


def _iter_geometries(shp_path: Path):
    """Yield geometries with a cartopy/shapely-like interface.

    With cartopy: shapely geometries as before. Without: pyshp records
    wrapped so the two extractors below see the same ``geom_type`` /
    ``coords`` / ``exterior`` shape. Shapefile polygon rings are
    clockwise for outer rings and counter-clockwise for holes.
    """
    try:
        from cartopy.io.shapereader import Reader
        yield from Reader(str(shp_path)).geometries()
        return
    except ImportError:
        pass
    import shapefile  # pyshp

    class _Ring:
        def __init__(self, pts):
            self.coords = pts

    class _Geom:
        def __init__(self, kind, parts):
            self.geom_type = kind
            self._parts = parts
            if kind == "Polygon":
                self.exterior = _Ring(parts[0])
                self.interiors = [_Ring(p) for p in parts[1:]]
            elif kind == "MultiPolygon":
                self.geoms = [_Geom("Polygon", [p]) for p in parts]
            elif kind == "MultiLineString":
                self.geoms = [_Geom("LineString", [p]) for p in parts]
            else:
                self.coords = parts[0]

    def _signed_area(pts):
        a = 0.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            a += x0 * y1 - x1 * y0
        return a / 2.0

    for shp in shapefile.Reader(str(shp_path)).shapes():
        pts = [tuple(p[:2]) for p in shp.points]
        parts = list(shp.parts) + [len(pts)]
        rings = [pts[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]
        if shp.shapeType in (shapefile.POLYLINE, shapefile.POLYLINEZ):
            yield _Geom("MultiLineString", rings)
        else:
            # Outer rings (clockwise, negative signed area) each become a
            # polygon; holes are dropped — matching the cartopy path, which
            # only ever reads ``exterior`` for the land mask.
            outers = [r for r in rings if _signed_area(r) < 0] or rings
            yield _Geom("MultiPolygon", outers)


def _extract_lonlat_polylines(resolution: str = "110m"):
    shp_path = _natural_earth_path(resolution, "physical", "coastline")
    for geom in _iter_geometries(shp_path):
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
    """(H, W) pixels matching what ``_to_pixel_grid`` produces.

    Latitude is inclusive of both endpoints — the ECMWF grid contains
    -90 and 90 cells, so a (lat_min, lat_max) crop selects every cell
    between them and the count is ``(lat_max - lat_min) / 0.25 + 1``.

    Longitude is *almost* the same, except the +180° cell wraps to
    -180° in our [-180, 180) frame after the longitude rotation in
    ``_to_pixel_grid``. For an extent that reaches lon_max = 180, the
    cell at +180 is selected as -180 elsewhere and so doesn't appear
    in the cropped slab — the width is one cell shorter than the
    inclusive count. This off-by-one breaks the apply-coastlines
    shape check and silently falls back to a flat-gray base map, so
    handle it here.
    """
    if extent is None:
        return _GLOBAL_DIMS
    lon_min, lon_max, lat_min, lat_max = extent
    h = int(round((lat_max - lat_min) / _GRID_DEG)) + 1
    w = int(round((lon_max - lon_min) / _GRID_DEG)) + 1
    if lon_max >= 180.0:
        w -= 1
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
        # width=1 — the chosen aesthetic for the analysis chart: the
        # thinnest possible line that still traces geography, drawn
        # in #050508 on top of the native composite (no LANCZOS
        # round-trip) so even a single pixel reads as a definite
        # boundary. An earlier widening to 2 px was a symptomatic
        # workaround for the base-map shape mismatch that hid the
        # land/sea cue entirely — once that bug was fixed the
        # original 1 px width became readable again.
        draw.line(list(zip(xs.tolist(), ys.tolist())), fill=255, width=1)
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
        # Standard NH polar convention (looking DOWN at the North
        # Pole from above space): 0° lon at the top of the image,
        # 90°E to the LEFT, 180° at the bottom, 90°W to the RIGHT.
        # Earth rotates W→E, which appears counter-clockwise from
        # above the NP — so on the chart, going east from 0° at top
        # means going CCW, which puts east on the left. ECMWF / JMA
        # / NOAA all draw NH polar this way; the older 'east to the
        # right' layout that the renderer produced before this fix
        # was effectively a view from below the SP looking up, which
        # is the wrong handedness for a NH chart.
        # arctan2(-dx, -dy): top→0°, left→+90°E, bottom→±180°, right→-90°.
        lon_rad = np.arctan2(-dx, -dy)
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
            # Standard NH polar (top-down): 0° top, 90°E LEFT, 180°
            # bottom, 90°W RIGHT. Inverse of `_polar_lookup`'s
            # arctan2(-dx, -dy): x = -sin(lon), y = -cos(lon).
            x = cx - r_norm * radius * np.sin(lon_rad)
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
    shp_path = _natural_earth_path(resolution, "physical", "land")
    for geom in _iter_geometries(shp_path):
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
            # Standard NH polar: see _polar_project_polylines for the
            # full derivation; x has a negative sign on sin to put
            # east on the LEFT of the chart.
            x = cx - r_norm * radius * np.sin(lon_rad)
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
