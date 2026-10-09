# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time generator for ``_blank_masks.npz`` (see ``_blank.py``).

Rasterises ``_jp_eez_claims.geojson`` (Japan EEZ + every overlapping
claim / joint regime; Marine Regions v12, CC BY 4.0) into a boolean
mask for every region preset at that region's pixel dimensions, using
the same fill rasterisers as the land mask so the two line up cell for
cell. The mask is then dilated by one cell (0.25°, ≈ 25 km) so the
blank always covers the boundary rather than stopping short of it.

Run::

    python -m aiseed_weather.figures._precompute_eez_blank
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from aiseed_weather.figures._precompute_coastlines import (
    _rasterise_land_mask, _rasterise_polar_land_mask, _region_dims,
)

_SRC = Path(__file__).parent / "_jp_eez_claims.geojson"
_OUT = Path(__file__).parent / "_blank_masks.npz"
KEY = "jp_eez"


def _polygons() -> list[np.ndarray]:
    data = json.loads(_SRC.read_text(encoding="utf-8"))
    polys: list[np.ndarray] = []
    for feat in data["features"]:
        for poly in feat["geometry"]["coordinates"]:
            # Outer ring only. Holes inside an EEZ claim would be land
            # (the islands themselves) — blanking them too is the safe
            # side and avoids a doughnut around every islet.
            polys.append(np.asarray(poly[0], dtype=np.float32))
    return polys


def _dilate(mask: np.ndarray) -> np.ndarray:
    """One-cell 8-neighbour dilation (pure numpy)."""
    out = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    # np.roll wraps around; polygons here never touch the image edge
    # except on 'global', where the wrap is the real antimeridian and
    # Japan is far from it, so wrapped pixels are all False anyway.
    return out


def build(out_path: Path = _OUT) -> Path:
    from aiseed_weather.figures.regions import PRESETS

    polys = _polygons()
    masks: dict[str, np.ndarray] = {}
    for region in PRESETS:
        if region.projection in ("north_polar", "south_polar"):
            mask = _rasterise_polar_land_mask(polys, region.projection == "north_polar")
        else:
            h, w = _region_dims(region.extent)
            extent = (-180.0, 180.0, -90.0, 90.0) if region.extent is None else region.extent
            mask = _rasterise_land_mask(polys, extent, h, w)
        mask = _dilate(mask)
        masks[f"{KEY}__{region.key}"] = mask
        print(f"  {KEY}__{region.key:14s} shape={mask.shape} cells={int(mask.sum())}")
    np.savez_compressed(out_path, **masks)
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


if __name__ == "__main__":
    build()
