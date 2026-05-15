# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Per-variable chart specs.

Read .agents/skills/chart-base-design and review ``_chart_spec.py``
before adding a new variable. The spec captures everything the
layered renderer needs that varies per variable:

  * extractor (xarray var name + unit conversion)
  * vmin / vmax + anchors (palette tied to legend ticks)
  * transparency, dry-threshold cutoff for one-sided variables
  * isolines spec when the field is synoptic-scale smooth

Importing this module side-effect-registers every spec into the
``SPECS`` dict in ``_chart_spec``.  Map view code looks up by
layer_key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from aiseed_weather.figures._chart_spec import (
    ChartSpec, IsolineSpec, register,
)

if TYPE_CHECKING:
    import xarray as xr


# ── Extractor helpers ───────────────────────────────────────────────


def _squeeze_2d(arr: np.ndarray) -> np.ndarray:
    """Squeeze a leading 1-step time/level axis so 3D fields with a
    trivial first dim become 2D (lat, lon). 2D arrays pass through."""
    while arr.ndim > 2:
        arr = arr[0]
    return arr


def _take(*names: str):
    """Return the first matching variable from a dataset, raw (no unit
    conversion). Useful for variables whose decoded name varies across
    cfgrib versions ("t2m" vs "2t", etc.)."""
    def _fn(ds: "xr.Dataset") -> np.ndarray:
        for n in names:
            if n in ds.data_vars:
                return _squeeze_2d(np.asarray(ds[n].values, dtype=np.float32))
        raise ValueError(
            f"None of {names!r} present in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return _fn


def _take_kelvin_to_c(*names: str):
    """As _take, then K → °C."""
    inner = _take(*names)
    def _fn(ds): return inner(ds) - 273.15
    return _fn


def _take_at_level(name: str, level: float):
    """Pull a pressure-level field and select one level. Used for the
    upper-air specs (gh500, t850, …)."""
    def _fn(ds: "xr.Dataset") -> np.ndarray:
        if name not in ds.data_vars:
            raise ValueError(
                f"No {name!r} in dataset; vars={list(ds.data_vars)}",
            )
        var = ds[name]
        for axis in ("isobaricInhPa", "level"):
            if axis in var.dims:
                return _squeeze_2d(
                    np.asarray(var.sel({axis: level}).values, dtype=np.float32)
                )
        raise ValueError(
            f"{name!r} has no level axis to select {level} from "
            f"(dims={var.dims})",
        )
    return _fn


def _wind_speed(u_names: tuple[str, ...], v_names: tuple[str, ...]):
    """Combine surface u and v components into a scalar wind speed."""
    u_extract = _take(*u_names)
    v_extract = _take(*v_names)
    def _fn(ds: "xr.Dataset") -> np.ndarray:
        u = u_extract(ds)
        v = v_extract(ds)
        return np.hypot(u, v)
    return _fn


def _wind_speed_at_level(u_name: str, v_name: str, level: float):
    """Pressure-level wind speed — select one level on u and v first."""
    u_extract = _take_at_level(u_name, level)
    v_extract = _take_at_level(v_name, level)
    def _fn(ds: "xr.Dataset") -> np.ndarray:
        u = u_extract(ds)
        v = v_extract(ds)
        return np.hypot(u, v)
    return _fn


# ── MSL pressure ────────────────────────────────────────────────────
# Diverging palette anchored at the Windy legend ticks (every 10 hPa
# from 990 to 1030). Synoptic-scale field → isobars at 2 hPa thin,
# 20 hPa bold, with value pills on the bold ones.
MSL = register(ChartSpec(
    layer_key="msl",
    label="msl",
    extractor=lambda ds: _squeeze_2d(np.asarray(ds["msl"].values, dtype=np.float32)) / 100.0,
    vmin=990.0,
    vmax=1030.0,
    anchors=(
        (990.0,  (38, 110, 55)),
        (1000.0, (115, 180, 110)),
        (1010.0, (230, 210, 165)),
        (1020.0, (215, 140, 75)),
        (1030.0, (135, 55, 25)),
    ),
    legend_ticks=(990.0, 1000.0, 1010.0, 1020.0, 1030.0),
    transparency=0.30,
    isolines=IsolineSpec(thin_interval=2.0, bold_interval=20.0),
))


# ── 2 m temperature ─────────────────────────────────────────────────
# Surface boundary-layer field — orography / urban / coastline drive
# strong sub-grid gradients, so no isotherms (would draw a wall of
# parallel lines along every coast in summer afternoons). Anchors at
# Windy legend ticks every 10 °C from -20 to 40.
T2M = register(ChartSpec(
    layer_key="t2m",
    label="t2m",
    extractor=_take_kelvin_to_c("t2m", "2t"),
    vmin=-20.0,
    vmax=40.0,
    anchors=(
        (-20.0, (50, 30, 165)),
        (-10.0, (40, 115, 225)),
        (0.0,   (230, 225, 215)),
        (10.0,  (190, 220, 100)),
        (20.0,  (245, 200, 80)),
        (30.0,  (220, 110, 60)),
        (40.0,  (135, 30, 30)),
    ),
    legend_ticks=(-20.0, -10.0, 0.0, 10.0, 20.0, 30.0, 40.0),
    transparency=0.30,
))


# ── Total precipitation (cumulative since run start, mm) ────────────
# Windy 3-hour precipitation legend at 1.5 / 2 / 3 / 7 / 10 / 20 / 30
# mm. Dry pixels (< 1 mm) pass through to the base map so the
# land/sea cue is never washed by trace-amount tint.
TP = register(ChartSpec(
    layer_key="tp",
    label="tp",
    extractor=lambda ds: _squeeze_2d(np.asarray(ds["tp"].values, dtype=np.float32)) * 1000.0,
    vmin=0.0,
    vmax=30.0,
    anchors=(
        (1.5,  (170, 220, 230)),
        (2.0,  (120, 195, 220)),
        (3.0,  (60, 140, 200)),
        (7.0,  (60, 175, 105)),
        (10.0, (220, 215, 75)),
        (20.0, (230, 145, 55)),
        (30.0, (190, 80, 140)),
    ),
    legend_ticks=(1.5, 2.0, 3.0, 7.0, 10.0, 20.0, 30.0),
    transparency=0.20,
    dry_threshold=1.0,
))


# ── Total precipitation rate (instantaneous, mm/h) ──────────────────
# Same palette / numerical ticks as TP — the bins read as the same
# precipitation intensity in both contexts; only the unit on the
# legend label differs.
# IMPORTANT: must NOT share this palette with the future JMA radar
# layer (see chart-base-design 'palette differentiation by data
# source').
TPRATE = register(ChartSpec(
    layer_key="tprate",
    label="tprate",
    extractor=lambda ds: _squeeze_2d(np.asarray(ds["tprate"].values, dtype=np.float32)) * 3600.0,
    vmin=TP.vmin,
    vmax=TP.vmax,
    anchors=TP.anchors,
    legend_ticks=TP.legend_ticks,
    transparency=TP.transparency,
    dry_threshold=TP.dry_threshold,
))


# ── 10 m wind speed ─────────────────────────────────────────────────
# Surface wind field — terrain / coast effects make isotachs at this
# level noisy, so no overlay lines. Sequential palette from calm
# (pale blue-gray) through moderate (green / yellow) to gale-force
# (red / dark purple). Anchors at Beaufort-friendly breakpoints.
WIND10M = register(ChartSpec(
    layer_key="wind10m",
    label="wind10m",
    extractor=_wind_speed(("u10", "10u"), ("v10", "10v")),
    vmin=0.0,
    vmax=40.0,
    anchors=(
        (0.0,   (220, 230, 235)),   # near-calm, pale
        (5.0,   (140, 200, 215)),   # light breeze, cyan
        (10.0,  (90, 180, 130)),    # moderate, green
        (15.0,  (215, 210, 90)),    # fresh, yellow
        (20.0,  (235, 145, 55)),    # strong, orange
        (25.0,  (210, 60, 60)),     # gale, red
        (40.0,  (110, 30, 110)),    # storm, dark purple
    ),
    legend_ticks=(0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0),
    transparency=0.30,
    dry_threshold=2.0,
))


# ── 10 m gust (max wind gust) ───────────────────────────────────────
# Wider range than mean wind — gusts in extratropical lows reach
# 30-50 m/s, typhoons 60+. Otherwise the same shading family.
GUST10M = register(ChartSpec(
    layer_key="gust",
    label="gust",
    extractor=_take("10fg", "fg10"),
    vmin=0.0,
    vmax=60.0,
    anchors=(
        (0.0,  (220, 230, 235)),
        (10.0, (140, 200, 215)),
        (20.0, (90, 180, 130)),
        (30.0, (215, 210, 90)),
        (40.0, (235, 145, 55)),
        (50.0, (210, 60, 60)),
        (60.0, (110, 30, 110)),
    ),
    legend_ticks=(0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0),
    transparency=0.30,
    dry_threshold=5.0,
))


# ── 500 hPa geopotential height ─────────────────────────────────────
# The classic synoptic chart variable. ECMWF/GFS conventions: 60 gpm
# isohypses (5640, 5700, 5760, …) — bold every 300 gpm. Field is
# smooth above the boundary layer so isolines carry real meaning.
# Diverging-by-anchor palette centred on the climatological 5700 gpm
# for mid-latitude winter / 5820 for summer; we anchor at 5700 as a
# middle-of-the-road choice. Display range covers polar lows to
# subtropical ridges.
GH500 = register(ChartSpec(
    layer_key="gh500",
    label="gh500",
    extractor=lambda ds: _take_at_level("gh", 500.0)(ds),  # already in gpm
    vmin=4920.0,
    vmax=6000.0,
    anchors=(
        (4920.0, (50, 30, 165)),    # deep polar trough — saturated indigo
        (5280.0, (40, 115, 225)),   # cold pool — blue
        (5520.0, (115, 180, 200)),  # mid-trough — cool cyan
        (5700.0, (230, 220, 180)),  # climatological mean — warm pale
        (5880.0, (215, 160, 80)),   # subtropical ridge axis — tan
        (6000.0, (135, 55, 25)),    # warm-side extreme — deep brown
    ),
    legend_ticks=(4920.0, 5280.0, 5520.0, 5700.0, 5880.0, 6000.0),
    transparency=0.30,
    isolines=IsolineSpec(thin_interval=60.0, bold_interval=300.0),
))


# ── 850 hPa temperature ─────────────────────────────────────────────
# Above the boundary layer, isotherms are physically meaningful
# (frontal positions, thermal troughs / ridges). 3 °C thin, 15 °C
# bold per the WMO synoptic chart convention.
T850 = register(ChartSpec(
    layer_key="t850",
    label="t850",
    extractor=lambda ds: _take_at_level("t", 850.0)(ds) - 273.15,
    vmin=-30.0,
    vmax=30.0,
    anchors=(
        (-30.0, (40, 25, 130)),
        (-15.0, (60, 130, 215)),
        (0.0,   (230, 225, 215)),
        (15.0,  (235, 195, 80)),
        (30.0,  (155, 35, 30)),
    ),
    legend_ticks=(-30.0, -15.0, 0.0, 15.0, 30.0),
    transparency=0.30,
    isolines=IsolineSpec(thin_interval=3.0, bold_interval=15.0),
))


# ── 250 hPa wind speed (jet stream) ─────────────────────────────────
# Upper-tropospheric jet axis is a synoptic feature; speed shading
# alone reads as the jet axis position. Threshold 30 m/s = the
# meteorological boundary for "jet streak" — below that we'd be
# looking at non-jet flow and the chart should let the base show.
WIND250 = register(ChartSpec(
    layer_key="wind250",
    label="wind250",
    extractor=_wind_speed_at_level("u", "v", 250.0),
    vmin=0.0,
    vmax=100.0,
    anchors=(
        (0.0,   (200, 225, 235)),
        (30.0,  (140, 200, 215)),
        (50.0,  (90, 180, 130)),
        (70.0,  (215, 210, 90)),
        (90.0,  (235, 100, 60)),
        (100.0, (135, 30, 30)),
    ),
    legend_ticks=(0.0, 30.0, 50.0, 70.0, 90.0, 100.0),
    transparency=0.30,
    dry_threshold=30.0,
))


# ── 2 m dewpoint ────────────────────────────────────────────────────
# Surface moisture; not isoline-tractable (sub-grid land-sea moisture
# contrast). Similar palette range to t2m but slightly cooler bias
# because high dewpoints (>25) are unusual.
D2M = register(ChartSpec(
    layer_key="d2m",
    label="d2m",
    extractor=_take_kelvin_to_c("d2m", "2d"),
    vmin=-30.0,
    vmax=30.0,
    anchors=(
        (-30.0, (50, 30, 140)),
        (-15.0, (55, 130, 215)),
        (0.0,   (215, 225, 220)),
        (10.0,  (140, 215, 165)),
        (20.0,  (215, 200, 90)),
        (30.0,  (140, 60, 50)),
    ),
    legend_ticks=(-30.0, -15.0, 0.0, 10.0, 20.0, 30.0),
    transparency=0.30,
))


# ── Total cloud cover (fraction 0..1) ───────────────────────────────
# Sequential pale-to-white. Treat the data overlay as a veil: high
# transparency overall, only dense cloud (> 0.5 fraction) reads as
# clearly white. Dry threshold at 0.1 (< 10 % cloud) so clear sky
# shows the base map cleanly.
TCC = register(ChartSpec(
    layer_key="tcc",
    label="tcc",
    extractor=_take("tcc"),
    vmin=0.0,
    vmax=1.0,
    anchors=(
        (0.0,  (160, 160, 165)),     # pale gray
        (0.25, (190, 195, 200)),
        (0.5,  (220, 222, 225)),
        (0.75, (240, 242, 245)),
        (1.0,  (255, 255, 255)),     # opaque white = thick overcast
    ),
    legend_ticks=(0.0, 0.25, 0.5, 0.75, 1.0),
    transparency=0.40,
    dry_threshold=0.1,
))


# ── Most-unstable CAPE ──────────────────────────────────────────────
# Convective available potential energy — boundary-layer field with
# patchy distribution, no isolines. Anchors at common operational
# thresholds: 500 (weak instability), 1000 (moderate), 2000 (strong),
# 4000 (extreme).
MUCAPE = register(ChartSpec(
    layer_key="mucape",
    label="mucape",
    extractor=_take("mucape"),
    vmin=0.0,
    vmax=4000.0,
    anchors=(
        (0.0,    (220, 225, 215)),
        (500.0,  (215, 215, 110)),
        (1000.0, (225, 170, 80)),
        (2000.0, (215, 100, 60)),
        (3000.0, (185, 50, 90)),
        (4000.0, (110, 30, 130)),
    ),
    legend_ticks=(0.0, 500.0, 1000.0, 2000.0, 3000.0, 4000.0),
    transparency=0.30,
    dry_threshold=100.0,
))


# ── Total column water vapour ───────────────────────────────────────
# Atmospheric moisture content (kg/m² = mm of water). Useful for
# locating moisture plumes, atmospheric rivers. Sequential
# pale-cyan to deep blue/green.
TCWV = register(ChartSpec(
    layer_key="tcwv",
    label="tcwv",
    extractor=_take("tcwv"),
    vmin=0.0,
    vmax=70.0,
    anchors=(
        (0.0,  (215, 220, 215)),
        (10.0, (180, 215, 220)),
        (20.0, (130, 195, 215)),
        (30.0, (80, 165, 210)),
        (40.0, (55, 135, 195)),
        (55.0, (45, 165, 130)),
        (70.0, (40, 110, 80)),
    ),
    legend_ticks=(0.0, 10.0, 20.0, 30.0, 40.0, 55.0, 70.0),
    transparency=0.30,
))


# Public registry — re-exported for convenience.
from aiseed_weather.figures._chart_spec import SPECS  # noqa: E402, F401
