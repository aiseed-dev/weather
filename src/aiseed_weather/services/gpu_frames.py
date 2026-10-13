# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Data for the GPU map view (the ``flet-gpufield`` control).

The GPU view takes the value grid itself — not an image — and colours,
interpolates and animates it on the GPU. This module turns cached
forecast files into what the control needs per step:

* the shaded value, packed as 16-bit into RG of an RGBA texture
  (A = valid), normalised to the layer's palette range,
* 10 m or pressure-level u/v as int16 (0.01 m/s) for the particles,
* isolines (thin / bold) and their value labels as (lon, lat) vertices.

Values, palettes, transparency, the dry cutoff and isoline intervals
come from the same ``ChartSpec`` the PNG figures use, so a step looks
the same in both views (the extractor does the unit conversion).

Speed: a bulk ECMWF GRIB is scanned with ``headers_only`` (184 messages
in ~0.13 s, cached per file) and only the needed messages are decoded
(~7 ms each). ``cfgrib`` costs 0.2–1.6 s per open, so the figure path's
decoder is not used here. Mirror packs (.nc) go through
``forecast_service.decode_kind``.

No Flet imports: this is a service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from aiseed_weather.figures.regions import Region

logger = logging.getLogger(__name__)

RES = 0.25                # ECMWF Open Data grid
N_LAT, N_LON = 721, 1440  # rows 90°N → 90°S, columns 0°E → 359.75°E

_FIGURES = Path(__file__).resolve().parent.parent / "figures"


# ── Domain: which part of the global grid the view gets ─────────────

@dataclass(frozen=True)
class GpuDomain:
    key: str
    projection: str        # plate_carree | polar_north | polar_south
    lon0: float            # first column (may be negative, e.g. Europe)
    lat0: float            # first row (northern edge)
    stride: int            # 1 = 0.25°, 2 = 0.5°
    nx: int
    ny: int
    periodic: bool         # wraps around in longitude
    center_lon: float      # initial view (polar: longitude at the bottom)
    center_lat: float
    px_per_deg: float

    @property
    def dlon(self) -> float:
        return RES * self.stride

    @property
    def dlat(self) -> float:
        return RES * self.stride

    def crop(self, a: np.ndarray) -> np.ndarray:
        """Cut this domain out of a global (721, 1440) array."""
        j0 = int(round((90.0 - self.lat0) / RES))
        rows = a[j0:j0 + self.ny * self.stride:self.stride]
        i0 = int(round((self.lon0 % 360.0) / RES))
        cols = (i0 + np.arange(self.nx) * self.stride) % N_LON
        return rows[:, cols]

    def lons(self) -> np.ndarray:
        return self.lon0 + np.arange(self.nx) * self.dlon

    def lats(self) -> np.ndarray:
        return self.lat0 - np.arange(self.ny) * self.dlat


def domain_for(region: "Region") -> GpuDomain:
    """The grid the GPU view gets for a map region."""
    if region.projection in ("north_polar", "south_polar"):
        north = region.projection == "north_polar"
        # Keep 10° beyond the region edge so panning out does not hit
        # the data edge immediately; 0.5° is plenty at polar scales.
        edge = 20.0
        ny = int(round((90.0 - edge) / 0.5)) + 1
        return GpuDomain(
            key=region.key,
            projection="polar_north" if north else "polar_south",
            lon0=0.0, lat0=90.0 if north else -edge, stride=2,
            nx=720, ny=ny, periodic=True,
            center_lon=135.0, center_lat=90.0 if north else -90.0, px_per_deg=7.0,
        )
    if region.extent is None:
        return _global_domain(region.key)
    lon_min, lon_max, lat_min, lat_max = region.extent
    west, east = lon_min - 5.0, lon_max + 5.0
    north, south = min(90.0, lat_max + 5.0), max(-90.0, lat_min - 5.0)
    if east - west >= 359.0:
        return _global_domain(region.key)
    span_lon, span_lat = east - west, north - south
    stride = 1 if span_lon * span_lat <= 120.0 * 80.0 else 2
    step = RES * stride
    lon0 = np.floor(west / RES) * RES
    lat0 = min(90.0, np.ceil(north / RES) * RES)
    return GpuDomain(
        key=region.key, projection="plate_carree",
        lon0=float(lon0), lat0=float(lat0), stride=stride,
        nx=int(span_lon / step) + 1, ny=int((lat0 - south) / step) + 1,
        periodic=False,
        center_lon=(lon_min + lon_max) / 2.0, center_lat=(lat_min + lat_max) / 2.0,
        px_per_deg=float(np.clip(1100.0 / (lon_max - lon_min), 2.0, 60.0)),
    )


def _global_domain(key: str) -> GpuDomain:
    return GpuDomain(
        key=key, projection="plate_carree", lon0=0.0, lat0=90.0, stride=2,
        nx=720, ny=361, periodic=True,
        center_lon=137.0, center_lat=20.0, px_per_deg=4.0,
    )


# ── Layers the GPU view can draw ─────────────────────────────────────

Need = tuple[str, str, int]   # (GRIB shortName, typeOfLevel, level)

_SFC_WIND: tuple[Need, Need] = (("10u", "heightAboveGround", 10), ("10v", "heightAboveGround", 10))
_MSL: Need = ("msl", "meanSea", 0)

# cfgrib-style dataset names, so ChartSpec extractors work unchanged.
_XR_NAME = {"2t": "t2m", "2d": "d2m", "10u": "u10", "10v": "v10"}


@dataclass(frozen=True)
class GpuLayer:
    key: str
    spec_key: str          # palette / range / isolines come from SPECS[spec_key]
    label: str
    unit: str
    needs: tuple[Need, ...]
    wind: tuple[Need, Need]
    speed_of: tuple[Need, Need] | None = None   # shaded value = |(u, v)|
    interval_precip: bool = False               # tp: amount since the previous step


def layer_for(key: str) -> GpuLayer | None:
    """The GPU layer for a catalog key, or None if the GPU view can't draw it yet."""
    from aiseed_weather.figures._chart_spec import SPECS
    import aiseed_weather.figures._chart_specs  # noqa: F401  (registers SPECS)
    from aiseed_weather.products.catalog import field_by_key

    try:
        fld = field_by_key(key)
    except KeyError:
        return None
    label, unit = fld.label_ja, fld.unit
    if key == "msl":
        return GpuLayer(key, "msl", label, unit, (_MSL,), _SFC_WIND)
    if key == "t2m":
        return GpuLayer(key, "t2m", label, unit, (("2t", "heightAboveGround", 2),), _SFC_WIND)
    if key == "d2m" and "d2m" in SPECS:
        return GpuLayer(key, "d2m", label, unit, (("2d", "heightAboveGround", 2),), _SFC_WIND)
    if key == "wind10m":
        return GpuLayer(key, "wind10m", label, unit, _SFC_WIND, _SFC_WIND, speed_of=_SFC_WIND)
    if key == "tp":
        return GpuLayer(key, "tp", "降水量（前の時刻から）", "mm", (("tp", "surface", 0),),
                        _SFC_WIND, interval_precip=True)
    level = fld.level
    if level is None:
        return None
    uv = (("u", "isobaricInhPa", level), ("v", "isobaricInhPa", level))
    for prefix, param in (("t", "t"), ("gh", "gh"), ("r", "r")):
        if key == f"{prefix}{level}" and key in SPECS:
            return GpuLayer(key, key, label, unit, ((param, "isobaricInhPa", level),), uv)
    if key == f"wind{level}":
        # Only wind250 has its own spec; other levels borrow its palette.
        return GpuLayer(key, "wind250", label, unit, uv, uv, speed_of=uv)
    return None


def kinds_for(layer: GpuLayer, msl_overlay: bool) -> tuple[str, ...]:
    """Which cache kinds (sfc / pl) a frame of this layer reads."""
    needs = set(layer.needs) | set(layer.wind)
    if msl_overlay:
        needs.add(_MSL)
    kinds = {"pl" if n[1] == "isobaricInhPa" else "sfc" for n in needs}
    return tuple(sorted(kinds))


# ── Static per-view data (palette, land, coastline) ──────────────────

def lut_rgba(layer: GpuLayer) -> tuple[bytes, float, float]:
    """256 × RGBA palette and its value range, from the layer's ChartSpec."""
    from aiseed_weather.figures._chart_spec import SPECS
    from aiseed_weather.figures._palette import build_continuous_lut

    spec = SPECS[layer.spec_key]
    rgb = build_continuous_lut(spec.anchors, spec.vmin, spec.vmax)
    alpha = np.full(256, int(round((1.0 - spec.transparency) * 255)), dtype=np.uint8)
    if spec.dry_threshold is not None:
        values = np.linspace(spec.vmin, spec.vmax, 256)
        alpha[values < spec.dry_threshold] = 0
    rgba = np.concatenate([rgb, alpha[:, None]], axis=1).astype(np.uint8)
    return rgba.tobytes(), float(spec.vmin), float(spec.vmax)


@lru_cache(maxsize=1)
def _global_land() -> np.ndarray:
    with np.load(_FIGURES / "_coastline_masks.npz") as z:
        land = z["global__land"]
    # The masks start at 180°W; the ECMWF grid starts at 0°.
    return np.roll(land, -N_LON // 2, axis=1)


def land_mask(domain: GpuDomain) -> bytes:
    return domain.crop(_global_land()).astype(np.uint8).tobytes()


@lru_cache(maxsize=1)
def _coast_sets() -> dict[str, np.ndarray]:
    with np.load(_FIGURES / "_gpu_coastlines.npz") as z:
        return {k: z[k] for k in z.files}


def coastline(domain: GpuDomain) -> bytes:
    """Coastline vertices for the domain (10 m around East Asia, else 50 m)."""
    sets = _coast_sets()
    if domain.projection != "plate_carree" or domain.periodic:
        pts = sets["global"]
        if domain.projection == "polar_north":
            pts = _clip(pts, lambda lon, lat: lat >= domain.lat0 - (domain.ny - 1) * domain.dlat - 5)
        elif domain.projection == "polar_south":
            pts = _clip(pts, lambda lon, lat: lat <= domain.lat0 + 5)
        return pts.astype("<f4").tobytes()
    west, east = domain.lon0, domain.lon0 + (domain.nx - 1) * domain.dlon
    north, south = domain.lat0, domain.lat0 - (domain.ny - 1) * domain.dlat
    in_east_asia = west >= 85.0 and east <= 195.0 and south >= -5.0 and north <= 70.0
    pts = sets["east_asia" if in_east_asia else "global"].copy()
    # Bring longitudes next to the domain (Europe uses negatives,
    # the North Pacific runs past 180°), then break lines at the wrap.
    base = west - 10.0
    lon = pts[:, 0]
    ok = ~np.isnan(lon)
    lon[ok] = (lon[ok] - base) % 360.0 + base
    jump = np.zeros(len(pts), bool)
    jump[1:] = np.abs(np.diff(lon)) > 180.0
    pts = np.insert(pts, np.nonzero(jump)[0], np.nan, axis=0)
    pts = _clip(pts, lambda lo, la: (lo >= west - 2) & (lo <= east + 2) & (la >= south - 2) & (la <= north + 2))
    return pts.astype("<f4").tobytes()


def _clip(pts: np.ndarray, keep) -> np.ndarray:
    """Replace vertices outside ``keep(lon, lat)`` with NaN (breaking lines)."""
    out = pts.copy()
    lon, lat = out[:, 0], out[:, 1]
    valid = ~np.isnan(lon)
    drop = valid.copy()
    drop[valid] = ~keep(lon[valid], lat[valid])
    out[drop] = np.nan
    # Collapse runs of NaN so the array stays small.
    nan = np.isnan(out[:, 0])
    keep_rows = ~(nan & np.concatenate([[True], nan[:-1]]))
    return out[keep_rows]


# ── Reading the cached files ─────────────────────────────────────────

@lru_cache(maxsize=256)
def _message_table(path_str: str, mtime_ns: int, size: int) -> dict[Need, tuple[int, int]]:
    """(shortName, typeOfLevel, level) → (offset, length) for a bulk GRIB."""
    import eccodes

    table: dict[Need, tuple[int, int]] = {}
    with open(path_str, "rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f, headers_only=True)) is not None:
            try:
                key = (
                    str(eccodes.codes_get(h, "shortName")),
                    str(eccodes.codes_get(h, "typeOfLevel")),
                    int(eccodes.codes_get(h, "level")),
                )
                table.setdefault(key, (int(eccodes.codes_get(h, "offset")),
                                       int(eccodes.codes_get(h, "totalLength"))))
            finally:
                eccodes.codes_release(h)
    return table


def _read_grib(path: Path, needs: set[Need]) -> dict[Need, np.ndarray]:
    import eccodes

    st = path.stat()
    table = _message_table(str(path), st.st_mtime_ns, st.st_size)
    out: dict[Need, np.ndarray] = {}
    with path.open("rb") as f:
        for need in needs:
            loc = table.get(need)
            if loc is None:
                continue
            f.seek(loc[0])
            h = eccodes.codes_new_from_message(f.read(loc[1]))
            try:
                ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
                vals = eccodes.codes_get_values(h).reshape(nj, ni).astype(np.float32)
                missing = eccodes.codes_get(h, "missingValue")
                if eccodes.codes_get(h, "bitmapPresent"):
                    vals[vals == missing] = np.nan
                out[need] = vals
            finally:
                eccodes.codes_release(h)
    return out


def _read_pack(path: Path, kind: str, needs: set[Need]) -> dict[Need, np.ndarray]:
    """Mirror pack (.nc): through the same decode the figures use."""
    from aiseed_weather.services.forecast_service import decode_kind

    ds = decode_kind(path, kind)
    out: dict[Need, np.ndarray] = {}
    try:
        for need in needs:
            short, tol, level = need
            name = _XR_NAME.get(short, short)
            if name not in ds.data_vars:
                continue
            var = ds[name]
            if tol == "isobaricInhPa":
                if "isobaricInhPa" not in var.dims:
                    continue
                var = var.sel(isobaricInhPa=float(level))
            arr = np.asarray(var.values, dtype=np.float32)
            while arr.ndim > 2:
                arr = arr[0]
            out[need] = arr
    finally:
        ds.close()
    return out


def read_needs(files: dict[str, Path], needs: set[Need]) -> dict[Need, np.ndarray]:
    """Read global (721, 1440) arrays for ``needs`` from the step's cached files."""
    out: dict[Need, np.ndarray] = {}
    by_kind: dict[str, set[Need]] = {}
    for n in needs:
        by_kind.setdefault("pl" if n[1] == "isobaricInhPa" else "sfc", set()).add(n)
    for kind, kneeds in by_kind.items():
        path = files.get(kind)
        if path is None:
            continue
        if path.suffix == ".nc":
            out.update(_read_pack(path, kind, kneeds))
        else:
            out.update(_read_grib(path, kneeds))
    return out


# ── Building one frame ───────────────────────────────────────────────

@dataclass
class FrameBytes:
    scalar: bytes
    wind: bytes | None = None
    contours: bytes | None = None
    contours_bold: bytes | None = None
    labels: bytes | None = None
    seconds: float = 0.0


@dataclass
class FrameBuilder:
    """Turns one step's cached files into FrameBytes for a layer + domain."""

    layer: GpuLayer
    domain: GpuDomain
    msl_overlay: bool = False
    _vrange: tuple[float, float] = field(default=(0.0, 1.0), init=False)

    def __post_init__(self) -> None:
        from aiseed_weather.figures._chart_spec import SPECS
        import aiseed_weather.figures._chart_specs  # noqa: F401

        self._spec = SPECS[self.layer.spec_key]
        self._msl_spec = SPECS["msl"]
        self._vrange = (float(self._spec.vmin), float(self._spec.vmax))

    def needs(self) -> set[Need]:
        n = set(self.layer.needs) | set(self.layer.wind)
        if self.msl_overlay and self.layer.key != "msl":
            n.add(_MSL)
        return n

    def build(self, files: dict[str, Path], prev_files: dict[str, Path] | None = None) -> FrameBytes:
        import time

        t0 = time.perf_counter()
        fields = read_needs(files, self.needs())
        value = self._value(fields, prev_files)
        d = self.domain
        crop = d.crop(value) if value is not None else np.full((d.ny, d.nx), np.nan, np.float32)
        frame = FrameBytes(scalar=_pack_scalar(crop, *self._vrange))

        u, v = (fields.get(n) for n in self.layer.wind)
        if u is not None and v is not None:
            frame.wind = _pack_wind(d.crop(u), d.crop(v))

        iso_field, iso_spec = None, None
        if self._spec.isolines is not None and value is not None:
            iso_field, iso_spec = crop, self._spec
        elif self.msl_overlay and self.layer.key != "msl" and _MSL in fields:
            iso_field = d.crop(self._extract(self._msl_spec, {_MSL: fields[_MSL]}))
            iso_spec = self._msl_spec
        if iso_field is not None:
            frame.contours, frame.contours_bold, frame.labels = _isolines(iso_field, d, iso_spec)
        frame.seconds = time.perf_counter() - t0
        return frame

    def _value(self, fields: dict[Need, np.ndarray], prev_files) -> np.ndarray | None:
        layer = self.layer
        if layer.speed_of is not None:
            u, v = (fields.get(n) for n in layer.speed_of)
            return None if u is None or v is None else np.hypot(u, v)
        if any(n not in fields for n in layer.needs):
            return None
        value = self._extract(self._spec, {n: fields[n] for n in layer.needs})
        if layer.interval_precip:
            if not prev_files:
                return np.zeros_like(value)
            prev = read_needs(prev_files, set(layer.needs))
            if any(n not in prev for n in layer.needs):
                return None
            value = value - self._extract(self._spec, {n: prev[n] for n in layer.needs})
            value = np.maximum(value, 0.0)   # packing noise can make tiny negatives
        return value

    @staticmethod
    def _extract(spec, fields: dict[Need, np.ndarray]) -> np.ndarray:
        """Run the ChartSpec extractor (unit conversion) on a minimal dataset."""
        import xarray as xr

        data_vars = {}
        for (short, tol, level), arr in fields.items():
            name = _XR_NAME.get(short, short)
            if tol == "isobaricInhPa":
                data_vars[name] = (("isobaricInhPa", "latitude", "longitude"), arr[None])
                coords_level = float(level)
            else:
                data_vars[name] = (("latitude", "longitude"), arr)
        coords = {}
        if any(t == "isobaricInhPa" for _, t, _ in fields):
            coords["isobaricInhPa"] = [coords_level]
        ds = xr.Dataset(data_vars, coords=coords)
        return np.asarray(spec.extractor(ds), dtype=np.float32)


def _pack_scalar(values: np.ndarray, vmin: float, vmax: float) -> bytes:
    span = (vmax - vmin) or 1.0
    with np.errstate(invalid="ignore"):
        q = np.clip((values - vmin) / span, 0.0, 1.0)
    iq = np.rint(np.nan_to_num(q) * 65535).astype(np.uint16)
    rgba = np.zeros(values.shape + (4,), np.uint8)
    rgba[..., 0] = iq >> 8
    rgba[..., 1] = iq & 255
    rgba[..., 3] = np.where(np.isfinite(values), 255, 0)
    return rgba.tobytes()


def _pack_wind(u: np.ndarray, v: np.ndarray) -> bytes:
    def q(a):
        return np.clip(np.rint(np.nan_to_num(a) * 100.0), -32767, 32767).astype("<i2")
    return q(u).tobytes() + q(v).tobytes()


def _isolines(field: np.ndarray, domain: GpuDomain, spec) -> tuple[bytes, bytes, bytes]:
    """Thin / bold isolines and bold-line labels, as in the PNG figures."""
    import contourpy
    from scipy.ndimage import gaussian_filter

    iso = spec.isolines
    z = np.where(np.isfinite(field), field, np.nanmean(field)).astype(np.float64)
    sigma = iso.smooth_sigma / domain.stride
    if sigma > 0:
        z = gaussian_filter(z, sigma=sigma, mode="wrap" if domain.periodic else "nearest")
    cgen = contourpy.contour_generator(x=domain.lons(), y=domain.lats(), z=z)
    step, bold_step = iso.thin_interval, iso.bold_interval
    lo = max(spec.vmin, float(np.nanmin(z)))
    hi = min(spec.vmax, float(np.nanmax(z)))
    start = step * np.floor(lo / step)
    min_vertices = max(4, iso.min_segment_vertices // domain.stride)
    thin, bold, labels = [], [], []
    for level in np.arange(start, hi + 1e-6, step):
        is_bold = abs(level / bold_step - round(level / bold_step)) < 1e-6
        for line in cgen.lines(float(level)):
            if len(line) < min_vertices:
                continue
            (bold if is_bold else thin).append(line)
            if is_bold and iso.with_pills:
                dx = np.abs(np.diff(line[:, 0]))
                j = int(np.argmax(dx))
                labels.append(((line[j, 0] + line[j + 1, 0]) / 2,
                               (line[j, 1] + line[j + 1, 1]) / 2, level))
    return _join(thin), _join(bold), np.asarray(labels, "<f4").tobytes()


def _join(lines: list[np.ndarray]) -> bytes:
    if not lines:
        return b""
    parts = []
    for ln in lines:
        parts.append(ln.astype("<f4"))
        parts.append(np.array([[np.nan, np.nan]], "<f4"))
    return np.concatenate(parts).tobytes()
