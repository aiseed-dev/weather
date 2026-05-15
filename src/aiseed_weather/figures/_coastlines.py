# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Runtime coastline overlay — pre-rasterised per region.

``_precompute_coastlines.py`` renders the Natural Earth 110m
coastlines into a boolean mask for every region preset, sized to the
exact ECMWF 0.25° grid each layer renders at. This module loads the
.npz once at import and exposes ``apply_coastlines`` which overlays
in-place via numpy fancy indexing — typically a few hundred
microseconds per frame.

No cartopy, no per-frame lat/lon → pixel transform, no PIL line
drawing. The polyline rasterisation already happened, in pixels,
on a developer machine; runtime only does the colour assign.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_CACHE_PATH = Path(__file__).parent / "_coastline_masks.npz"


def _load() -> dict[str, np.ndarray]:
    if not _CACHE_PATH.exists():
        return {}
    with np.load(_CACHE_PATH) as data:
        return {k: np.asarray(data[k], dtype=bool) for k in data.files}


# Module-level cache: loaded once at first import.
_MASKS: dict[str, np.ndarray] = _load()

# Thin near-black line — kept at 1 px so it traces the geography
# without covering data, and dark enough to read against every
# palette colour the renderers ship (cool-blue ends, warm-red ends,
# white-zero precipitation, gray base map). The previous near-white
# choice came from a commercial-app aesthetic and lost the line
# entirely on charts with a white-to-blue precipitation palette.
_COASTLINE_RGB: np.ndarray = np.array([24, 24, 28], dtype=np.uint8)  # #18181c


def apply_coastlines(rgb: np.ndarray, region_key: str) -> None:
    """Stamp coastlines onto an RGB array in place.

    No-op when no mask is cached for the region (e.g. user-defined
    custom bounds) or when the array shape doesn't match the
    precomputed mask (e.g. data at non-0.25° resolution). Both are
    expected fallbacks rather than errors.
    """
    mask = _MASKS.get(region_key)
    if mask is None:
        return
    if rgb.shape[:2] != mask.shape:
        return
    rgb[mask] = _COASTLINE_RGB
