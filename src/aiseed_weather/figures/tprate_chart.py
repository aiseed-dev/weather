# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""降水強度 (total precipitation rate) chart — thin wrapper.

Spec lives in ``_chart_specs.TPRATE``. Shares the Windy palette with
TP at the numerical level (same bins, just mm vs mm/h units); see
chart-base-design 'palette differentiation by data source' for why
this same family must NOT extend to the future JMA radar layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiseed_weather.figures._chart_specs import TPRATE
from aiseed_weather.figures._layered_renderer import render
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


def render_tprate(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
) -> bytes:
    return render(TPRATE, ds, region=region, run_id=run_id)
