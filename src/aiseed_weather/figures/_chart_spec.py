# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Declarative spec for a layered chart.

Read .agents/skills/chart-base-design before editing. Each variable
the renderer can draw is one ``ChartSpec`` value. The spec captures
the variable-specific bits (palette anchors, isoline interval,
transparency, etc.) while the shared pipeline in
``_layered_renderer`` does the actual rendering work.

This replaces the older per-variable file approach (msl_chart.py
etc.), each of which hand-coded the same four-layer composite from
scratch. After migration those modules become one-line wrappers that
call ``render(SPEC, …)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import xarray as xr


@dataclass(frozen=True)
class IsolineSpec:
    """When to draw isolines on top of a data overlay.

    A variable gets isolines when its field is synoptic-scale smooth
    and line shape carries the analysis information (MSL, geopotential,
    upper-air temperature). Variables that don't pass the physics test
    in chart-base-design omit this field — see the skill for examples.
    """

    thin_interval: float
    """Cadence of regular ('thin') isolines, in display units."""

    bold_interval: float
    """Every n-th line is drawn bolder. Must be a multiple of
    ``thin_interval`` for the renderer to line up correctly."""

    smooth_sigma: float = 3.0
    """Gaussian smoothing applied to the field before contouring.
    Suppresses small-scale noise without rounding off synoptic shape.
    σ=3 grid cells ≈ 0.75 ° at the ECMWF 0.25 ° grid."""

    min_segment_vertices: int = 30
    """Drop polyline fragments shorter than this many contourpy
    vertices. Short fragments are usually local bumps that clutter
    the chart without information."""

    with_pills: bool = True
    """Draw a pill label on each bold isoline. Useful for variables
    where the analyst needs the value at the line (pressure, height);
    can be turned off where the line itself is the information and
    a numerical label would be redundant."""


@dataclass(frozen=True)
class ChartSpec:
    """One chartable variable. Frozen so a global registry is safe to
    treat as immutable.

    Fields are calibration points, not laws of nature — expect them to
    move as the catalogue grows. See chart-base-design for the design
    principles each field encodes.
    """

    layer_key: str
    """Stable identifier — matches the catalog FIELDS key."""

    label: str
    """Goes in the PNG metadata ``Layer`` tag for downstream
    provenance / debugging."""

    extractor: Callable[["xr.Dataset"], np.ndarray]
    """Pull the value array (in display units) out of an xarray
    Dataset. The renderer expects shape (lat, lon); a 1-step time
    axis on the front is squeezed silently."""

    vmin: float
    vmax: float
    """Inclusive ends of the display range. The renderer clips norm
    to [0, 1] so data outside [vmin, vmax] saturates to the
    palette ends rather than glitching."""

    anchors: tuple[tuple[float, tuple[int, int, int]], ...]
    """Palette anchors — (data_value, (R, G, B)) pairs, with
    data_value in display units. ``np.interp`` builds a smooth
    256-entry LUT through these. Anchors are conventionally placed
    at the legend tick positions so 'tick value' and 'colour at that
    value' are visibly the same shade."""

    legend_ticks: tuple[float, ...]
    """Values to label on the legend bar. Usually identical to the
    anchor positions, but a variable can request denser or sparser
    ticks than the palette anchor grid."""

    transparency: float = 0.30
    """Data overlay transparency, 0 = opaque, 1 = invisible. Matches
    the Japanese 透明度 reading. Default 0.30 fits most variables;
    precipitation drops to 0.20 because dry pixels already skip the
    overlay, so wet pixels can read more strongly."""

    dry_threshold: float | None = None
    """Below this value, the data overlay is skipped entirely and the
    base map shows through unchanged. Used for one-sided variables
    where 'zero' should look like 'no data of interest here' rather
    than 'lightest colour on the gradient'. Precipitation uses 1.0,
    snow depth would use ~0.01 m, etc. ``None`` disables the cutoff."""

    isolines: IsolineSpec | None = None
    """``None`` means colour-only (no iso-line overlay). Set when the
    variable's field is synoptic-scale smooth and line shape carries
    information — see the chart-base-design 'isolines: physics-driven'
    section."""

    categorical: bool = False
    """When ``True``, the renderer uses ``np.digitize`` against the
    anchor positions instead of a continuous LUT lookup. Use for
    discrete-code variables like ``ptype`` (precipitation type), where
    interpolating between '1 = rain' and '3 = freezing rain' would
    paint a meaningless mid-tone over '2'. Each anchor position then
    represents 'the code at or above this value gets this colour'."""


# Global registry, populated lazily by import side-effects in
# _chart_specs.py. Map view code looks up specs by layer_key.
SPECS: dict[str, ChartSpec] = {}


def register(spec: ChartSpec) -> ChartSpec:
    """Add ``spec`` to the global registry and return it.

    Specs declared at module load time use this so a single name
    binding both defines the spec and registers it under its key.
    """
    SPECS[spec.layer_key] = spec
    return spec
