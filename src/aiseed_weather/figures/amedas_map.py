# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""AMeDAS snapshot as station markers over Japan.

One marker per station, coloured by the selected element, in the Japan
map frame shared with the radar figure (``_japan_map``).

Palettes: temperature and wind speed reuse the model charts' anchors
(``_chart_specs``) so the same colour means the same value across the
app. Precipitation uses JMA's own bins — observed precipitation must
not share the NWP palette (chart-base-design: palette differentiation
by data source), and JMA's scheme is what the user recognises.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize
from matplotlib.figure import Figure

from aiseed_weather.figures._chart_specs import T2M, WIND10M
from aiseed_weather.figures._fonts import configure_cjk_font
from aiseed_weather.figures._japan_map import (
    AXIS_FG,
    BG,
    EXTENT,
    JMA_PRECIP_BOUNDS,
    JMA_PRECIP_COLOURS,
    japan_axes,
)
from aiseed_weather.figures._palette import build_continuous_lut
from aiseed_weather.figures.footer import apply_footer

configure_cjk_font()

_JST = ZoneInfo("Asia/Tokyo")
_SUN_ANCHORS = ((0.0, (90, 96, 110)), (0.5, (205, 175, 90)), (1.0, (245, 210, 60)))
_HUMIDITY_ANCHORS = ((0.0, (190, 120, 60)), (50.0, (225, 225, 215)), (100.0, (40, 110, 190)))
_SNOW_ANCHORS = ((0.0, (225, 235, 245)), (50.0, (120, 170, 220)),
                 (150.0, (60, 80, 180)), (300.0, (110, 40, 140)))


@dataclass(frozen=True)
class AmedasElement:
    key: str                       # JmaAmedasService observation key
    label: str
    unit: str
    kind: str                      # "continuous" | "jma_precip"
    vmin: float = 0.0
    vmax: float = 1.0
    anchors: tuple = ()
    hide_zero: bool = False        # dry / no-snow stations are drawn small and gray


ELEMENTS: dict[str, AmedasElement] = {e.key: e for e in (
    AmedasElement("temp", "気温", "°C", "continuous", T2M.vmin, T2M.vmax, T2M.anchors),
    AmedasElement("prcp_1h", "前 1 時間降水量", "mm", "jma_precip", hide_zero=True),
    AmedasElement("prcp_24h", "前 24 時間降水量", "mm", "jma_precip", hide_zero=True),
    AmedasElement("wind_speed", "風速（10 分平均）", "m/s", "continuous",
                  WIND10M.vmin, 30.0, WIND10M.anchors),
    AmedasElement("humidity", "相対湿度", "%", "continuous", 0.0, 100.0, _HUMIDITY_ANCHORS),
    AmedasElement("sun_10m", "前 10 分間日照時間", "h", "continuous",
                  0.0, 1 / 6, tuple((v / 6, c) for v, c in _SUN_ANCHORS)),
    AmedasElement("snow_depth", "積雪深", "cm", "continuous", 0.0, 300.0, _SNOW_ANCHORS,
                  hide_zero=True),
)}


def _cmap_norm(el: AmedasElement):
    if el.kind == "jma_precip":
        return ListedColormap(JMA_PRECIP_COLOURS), BoundaryNorm(JMA_PRECIP_BOUNDS, 8)
    lut = build_continuous_lut(el.anchors, el.vmin, el.vmax) / 255.0
    return ListedColormap(lut), Normalize(el.vmin, el.vmax)


def render_amedas_map(lons: np.ndarray, lats: np.ndarray, values: np.ndarray, *,
                      element: str, valid: datetime, attribution: str, dpi: int = 100) -> bytes:
    """PNG of one AMeDAS element. ``valid`` is the snapshot time (aware)."""
    el = ELEMENTS[element]
    inside = ((lons >= EXTENT[0]) & (lons <= EXTENT[1]) &
              (lats >= EXTENT[2]) & (lats <= EXTENT[3]) & ~np.isnan(values))
    lons, lats, values = lons[inside], lats[inside], values[inside]
    fig = Figure(figsize=(8.0, 8.6), facecolor=BG)
    FigureCanvasAgg(fig)
    ax = japan_axes(fig, (0.07, 0.12, 0.80, 0.78))
    cmap, norm = _cmap_norm(el)
    zero = (values <= 0) if el.hide_zero else np.zeros(len(values), bool)
    ax.scatter(lons[zero], lats[zero], s=4, color="#c8ccd2", linewidths=0, zorder=2)
    order = np.argsort(np.abs(values[~zero] - (el.vmin + el.vmax) / 2))   # extremes on top
    sc = ax.scatter(lons[~zero][order], lats[~zero][order], c=values[~zero][order],
                    cmap=cmap, norm=norm, s=14, linewidths=0, zorder=3)
    cax = fig.add_axes((0.89, 0.12, 0.025, 0.78))
    cb = fig.colorbar(sc, cax=cax)
    cb.set_label(f"{el.label}（{el.unit}）", color=AXIS_FG, fontsize=9)
    cb.ax.tick_params(labelsize=8, colors=AXIS_FG)
    if el.kind == "jma_precip":
        cb.set_ticks(JMA_PRECIP_BOUNDS[:-1])
    utc = valid.astimezone(UTC)
    jst = valid.astimezone(_JST)
    title = f"アメダス {el.label}  {jst:%Y-%m-%d %H:%M} JST（{utc:%H:%M} UTC）"
    fig.text(0.07, 0.955, title, fontsize=12, color=AXIS_FG, ha="left", va="top", weight="bold")
    note = f"{len(values)} 地点" + ("。白っぽい小さな点は 0" if el.hide_zero else "")
    fig.text(0.07, 0.925, note, fontsize=8.5, color="#6b7078", ha="left", va="top")
    apply_footer(fig, data_source="JMA AMeDAS", run_id=f"{jst:%Y-%m-%d %H:%M} JST",
                 license_text=attribution, run_label="valid")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor(), metadata={
        "Title": title, "Software": "AIseed Weather v0.1 (https://aiseed.dev)",
        "Source": "JMA AMeDAS", "Copyright": attribution,
        "Description": f"AMeDAS {element} valid {utc:%Y-%m-%dT%H:%MZ}",
    })
    return buf.getvalue()
