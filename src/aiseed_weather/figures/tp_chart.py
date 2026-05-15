# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation chart — thin wrapper around the layered renderer.

Spec lives in ``_chart_specs.TP`` (Windy 3-hour palette, 1.5..30 mm).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiseed_weather.figures._chart_specs import TP
from aiseed_weather.figures._layered_renderer import render
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


def render_tp(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
    msl_overlay_ds: "xr.Dataset | None" = None,  # kept for API compat
) -> bytes:
    return render(TP, ds, region=region, run_id=run_id)
