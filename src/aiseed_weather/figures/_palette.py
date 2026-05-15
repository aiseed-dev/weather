# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Continuous 256-entry RGB LUT from a small set of anchors.

Read .agents/skills/chart-base-design — section 'Palette construction'.
Anchors are non-uniform in general (e.g. precipitation legend ticks
sit at 1.5, 2, 3, 7, 10, 20, 30 mm); ``np.interp`` handles the
non-uniform x-axis natively.

The cost of building a LUT is ~50 µs; specs cache theirs at import.
"""

from __future__ import annotations

import numpy as np


def build_continuous_lut(
    anchors: tuple[tuple[float, tuple[int, int, int]], ...],
    vmin: float,
    vmax: float,
) -> np.ndarray:
    """Linear-interpolate the anchors onto a (256, 3) uint8 LUT.

    ``anchors`` is a sequence of (value, (R, G, B)) pairs sorted by
    value. Values below ``vmin`` or above ``vmax`` saturate at the
    nearest anchor colour (np.interp's edge behaviour).
    """
    xs = np.array(
        [(v - vmin) / (vmax - vmin) for v, _ in anchors],
        dtype=np.float32,
    )
    rgb = np.array([c for _, c in anchors], dtype=np.float32)
    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.empty((256, 3), dtype=np.float32)
    for ch in range(3):
        out[:, ch] = np.interp(t, xs, rgb[:, ch])
    return np.clip(out, 0, 255).astype(np.uint8)


def palette_rgb_for(
    value: float,
    lut: np.ndarray,
    vmin: float,
    vmax: float,
) -> tuple[int, int, int]:
    """Pick one colour from a LUT at a specific data value.

    Used for pill label backgrounds — the pill on an isoline at value
    v gets the colour the data overlay paints at that value, so the
    pill is a visible part of the same scale as the surrounding fill.
    """
    norm = max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
    idx = int(round(norm * 255.0))
    r, g, b = lut[idx]
    return int(r), int(g), int(b)
