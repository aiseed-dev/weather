# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""MSL pressure chart — thin wrapper around the layered renderer.

The actual rendering logic lives in ``_layered_renderer.render``;
this module only exists for callers that import ``render_msl``
directly. Per-variable design choices are in ``_chart_specs.MSL``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiseed_weather.figures._chart_specs import MSL
from aiseed_weather.figures._layered_renderer import render
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


def render_msl(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
) -> bytes:
    return render(MSL, ds, region=region, run_id=run_id)
