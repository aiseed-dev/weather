# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Per-region neutral-gray base map.

Every chart now starts from a shared three-layer base:

    sea         (slightly darker neutral gray)
    land        (slightly lighter neutral gray)
    coastline   (thin near-black line)

Data overlays go on top of this. Where a renderer chooses to make
low-data-value pixels transparent (e.g. precipitation below
threshold), the base shows through and the analyst can still place
themselves on the map.

The land / coastline masks are precomputed offline by
``_precompute_coastlines.py``; this module just composes them into
an RGB array at the region's pixel dimensions.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_CACHE_PATH = Path(__file__).parent / "_coastline_masks.npz"


def _load_masks() -> dict[str, np.ndarray]:
    if not _CACHE_PATH.exists():
        return {}
    with np.load(_CACHE_PATH) as data:
        return {k: np.asarray(data[k], dtype=bool) for k in data.files}


_MASKS: dict[str, np.ndarray] = _load_masks()


# Three flat grays, no temperature tint. Tested for readability
# against every palette the renderer ships (cool-blue precipitation,
# diverging temperature, warm CAPE, etc.) — the base never competes
# for attention but is always there.
SEA_RGB = np.array([88, 92, 100], dtype=np.uint8)        # #585c64
LAND_RGB = np.array([118, 122, 128], dtype=np.uint8)     # #767a80
COASTLINE_RGB = np.array([24, 24, 28], dtype=np.uint8)   # #18181c


def base_map_rgb(region_key: str, *, with_coastline: bool = False) -> np.ndarray | None:
    """Build the gray sea / land RGB for ``region_key``.

    By default the coastline is NOT baked into the returned array.
    Renderers alpha-blend a data overlay over this base and then
    stamp the coastline on top via :func:`apply_coastlines` so the
    dark line stays at full strength instead of being diluted by the
    blend. Pass ``with_coastline=True`` only when the caller wants a
    standalone preview image with no data on top (e.g. a base-map
    debug PNG).

    Returns ``None`` when no mask is cached for the region.
    """
    coast = _MASKS.get(region_key)
    land = _MASKS.get(f"{region_key}__land")
    if coast is None or land is None:
        return None
    h, w = coast.shape
    rgb = np.empty((h, w, 3), dtype=np.uint8)
    rgb[:] = SEA_RGB
    rgb[land] = LAND_RGB
    if with_coastline:
        rgb[coast] = COASTLINE_RGB
    return rgb


def coastline_mask(region_key: str) -> np.ndarray | None:
    """Boolean mask of coastline pixels for compositing on top of data."""
    return _MASKS.get(region_key)


def land_mask(region_key: str) -> np.ndarray | None:
    """Boolean mask of land pixels (filled), for variables that need
    to mask land or sea (e.g. SST on sea only, soil temperature on
    land only)."""
    return _MASKS.get(f"{region_key}__land")
