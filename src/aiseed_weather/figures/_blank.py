# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Blank-out masks — areas a chart must NOT show data for.

The first (and so far only) blank set is ``jp_eez``: Japan's exclusive
economic zone including every overlapping claim and joint-regime area
(safe side). The overseas chart set published for time-j.net paints
this area flat gray so the charts never carry a forecast for Japanese
waters (気象業務法 line; worldtime-web design log K12/K13).

Mechanics mirror ``_coastlines``: ``_precompute_eez_blank.py`` rasterises
the polygons once per region preset at the exact 0.25° pixel grid and
ships ``_blank_masks.npz``; runtime is a numpy fancy-index fill.

Activation is a context variable rather than a parameter threaded
through every renderer: every chart path ends in
:func:`_coastlines.apply_coastlines`, which calls
:func:`apply_active_blank` first. Wrap the publisher's render calls in
``with blanking("jp_eez"):``. The app never activates it.

Safety: if a blank is active but no mask exists for the region (or the
shape doesn't match), rendering **raises** instead of silently
producing an unblanked chart.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import numpy as np

_CACHE_PATH = Path(__file__).parent / "_blank_masks.npz"


def _load() -> dict[str, np.ndarray]:
    if not _CACHE_PATH.exists():
        return {}
    with np.load(_CACHE_PATH) as data:
        return {k: np.asarray(data[k], dtype=bool) for k in data.files}


_MASKS: dict[str, np.ndarray] = _load()

# Flat light gray, clearly "no data" against the dark sea/land base and
# every data palette, without reading as a weather value.
BLANK_RGB: np.ndarray = np.array([214, 214, 218], dtype=np.uint8)  # #d6d6da

_active: ContextVar[str | None] = ContextVar("aiseed_blank", default=None)


@contextmanager
def blanking(key: str):
    """Activate blank set ``key`` for every render inside the block."""
    token = _active.set(key)
    try:
        yield
    finally:
        _active.reset(token)


def active_blank() -> str | None:
    return _active.get()


def has_mask(key: str, region_key: str) -> bool:
    return f"{key}__{region_key}" in _MASKS


def apply_blank(rgb: np.ndarray, region_key: str, key: str) -> None:
    mask = _MASKS.get(f"{key}__{region_key}")
    if mask is None:
        raise RuntimeError(
            f"blank {key!r} has no mask for region {region_key!r}; "
            "run python -m aiseed_weather.figures._precompute_eez_blank",
        )
    if rgb.shape[:2] != mask.shape:
        raise RuntimeError(
            f"blank {key!r}/{region_key!r}: mask shape {mask.shape} != "
            f"image {rgb.shape[:2]} (grid changed? re-run the precompute)",
        )
    rgb[mask] = BLANK_RGB


def apply_active_blank(rgb: np.ndarray, region_key: str) -> None:
    key = _active.get()
    if key is not None:
        apply_blank(rgb, region_key, key)
