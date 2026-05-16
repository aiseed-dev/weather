# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Time-series chart for the 地点 (point forecast) view.

Overlays the four data lanes the spec calls out:

  * **HRES forecast line** (black, solid) — past 3 + future 15 days
    of the deterministic ECMWF IFS HRES run.
  * **Climatology band** (gray, hatched) — p25..p75 across the
    past 30 years from the ERA5 archive. Spans the whole axis.
  * **Ensemble band** (blue, fill) — p10..p90 across the 51 IFS ENS
    members. Future side only.
  * **MSM reference line** (slate, dashed) — JMA Meso-Scale Model,
    Japan locations only, short-range.

Plus a 'now' vertical marker so the past / future split is read at a
glance.

Output is a PNG byte stream — same convention as map_view's
chart pipeline — that the UI displays through ``ft.Image``. The
caller renders inside ``asyncio.to_thread`` because matplotlib is
synchronous.

See docs/forecast-spec.md and .agents/skills/chart-base-design for
the visual principles (restrained palette, bands transparent enough
to overlap, single line carries the value).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import polars as pl

logger = logging.getLogger(__name__)


# Variable presentation table. Adding a new variable means adding one
# entry: (display_label, unit). The chart renderer never hardcodes
# 'temperature_2m'.
_VAR_INFO: dict[str, tuple[str, str]] = {
    "temperature_2m":     ("気温 / Temperature",       "°C"),
    "precipitation":      ("降水量 / Precipitation",    "mm/h"),
    "relative_humidity_2m": ("相対湿度 / Humidity",     "%"),
    "wind_speed_10m":     ("風速 / Wind speed",         "m/s"),
    "cloud_cover":        ("雲量 / Cloud cover",        "%"),
}


# Colours (chart-base-design: restrained palette, bands carry
# uncertainty by transparency rather than saturation).
_HRES_LINE = "#1c1c20"          # near-black
_MSM_LINE = "#56657a"           # slate, dashed
_CLIM_FILL = "#9aa0a8"          # neutral gray for the climatology band
_CLIM_LINE = "#5d6470"          # for the climatology mean line
_ENS_FILL = "#3478b8"           # cool blue, ensemble p10..p90
_NOW_LINE = "#b53a2a"           # warm red, 'now' vertical marker
_BG = "#f7f7f5"                 # very pale background
_AXIS_FG = "#202428"


def _setup_axes(ax, var_label: str, unit: str) -> None:
    ax.set_facecolor(_BG)
    ax.tick_params(colors=_AXIS_FG, labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(_AXIS_FG)
    ax.spines["bottom"].set_color(_AXIS_FG)
    ax.set_ylabel(f"{var_label} ({unit})", color=_AXIS_FG, fontsize=10)
    ax.grid(True, axis="y", color="#dddddd", linewidth=0.6, alpha=0.7)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=(0, 6, 12, 18)))


def _to_pydatetime(series) -> list[datetime]:
    """Polars Datetime column → list of timezone-aware Python datetimes.

    Matplotlib's date locators want naive or tz-aware Python datetimes;
    a Polars Datetime("us", time_zone="UTC") column already comes out
    as tz-aware datetime via ``to_list()``.
    """
    return series.to_list()


def render_point_forecast(
    *,
    location_name: str,
    variable: str,
    hres_joined: pl.DataFrame,
    msm_df: pl.DataFrame | None,
    ensemble_quantiles: pl.DataFrame | None,
    now_utc: datetime | None = None,
    width_in: float = 9.5,
    height_in: float = 3.6,
    dpi: int = 110,
) -> bytes:
    """Render one variable's time-series chart to PNG bytes.

    Synchronous. Caller is responsible for ``asyncio.to_thread`` —
    matplotlib's figure machinery is not safe to call from the event
    loop in the millisecond budget the UI wants.

    ``hres_joined`` is the climatology-joined HRES forecast (output
    of ``join_forecast_with_climatology``). The function pulls
    ``{variable}_p25 / _p75 / _mean`` columns when present; missing
    columns just skip the climatology band.
    """
    if variable not in _VAR_INFO:
        raise ValueError(f"Unknown variable {variable!r}")
    label, unit = _VAR_INFO[variable]

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor(_BG)
    _setup_axes(ax, label, unit)

    if hres_joined.is_empty():
        ax.text(
            0.5, 0.5, "データなし", transform=ax.transAxes,
            ha="center", va="center", color=_AXIS_FG, fontsize=12,
        )
        ax.set_xticks([])
        return _save_to_png(fig)

    ts = _to_pydatetime(hres_joined["timestamp"])

    # ── Climatology band (mean ± std, p25..p75) ──────────────────
    mean_col = f"{variable}_mean"
    p25_col = f"{variable}_p25"
    p75_col = f"{variable}_p75"
    if all(c in hres_joined.columns for c in (p25_col, p75_col)):
        # Drop rows where the band is null (no archive data yet for
        # that calendar day). fill_between needs aligned arrays so we
        # use the same index throughout.
        p25 = hres_joined[p25_col].to_list()
        p75 = hres_joined[p75_col].to_list()
        ax.fill_between(
            ts, p25, p75,
            color=_CLIM_FILL, alpha=0.28,
            linewidth=0,
            label="平年 p25..p75",
        )
    if mean_col in hres_joined.columns:
        mean_vals = hres_joined[mean_col].to_list()
        ax.plot(
            ts, mean_vals,
            color=_CLIM_LINE, linestyle=":", linewidth=1.0,
            label="平年 mean",
        )

    # ── Ensemble band (future side only) ────────────────────────
    if (
        ensemble_quantiles is not None
        and not ensemble_quantiles.is_empty()
        and "variable" in ensemble_quantiles.columns
    ):
        ens = ensemble_quantiles.filter(pl.col("variable") == variable)
        if not ens.is_empty():
            ens_ts = _to_pydatetime(ens["timestamp"])
            ax.fill_between(
                ens_ts,
                ens["p10"].to_list(),
                ens["p90"].to_list(),
                color=_ENS_FILL, alpha=0.18,
                linewidth=0,
                label="ENS p10..p90",
            )

    # ── HRES deterministic line (over everything) ────────────────
    if variable in hres_joined.columns:
        ax.plot(
            ts, hres_joined[variable].to_list(),
            color=_HRES_LINE, linewidth=1.6,
            label="HRES",
        )

    # ── MSM dashed reference ────────────────────────────────────
    if (
        msm_df is not None
        and not msm_df.is_empty()
        and variable in msm_df.columns
    ):
        ax.plot(
            _to_pydatetime(msm_df["timestamp"]),
            msm_df[variable].to_list(),
            color=_MSM_LINE, linewidth=1.0, linestyle="--",
            label="MSM (参考)",
        )

    # ── Now marker ──────────────────────────────────────────────
    now = now_utc or datetime.now(timezone.utc)
    ax.axvline(now, color=_NOW_LINE, linewidth=0.8, linestyle="-", alpha=0.7)
    # Small "現在" label at top of axis at the now line
    ax.text(
        now, ax.get_ylim()[1], " 現在", color=_NOW_LINE,
        va="top", ha="left", fontsize=8,
    )

    ax.set_title(
        f"{location_name} — {label}",
        color=_AXIS_FG, fontsize=12, loc="left",
    )
    leg = ax.legend(
        loc="upper right", fontsize=8, framealpha=0.85, ncol=4,
    )
    if leg is not None:
        leg.get_frame().set_facecolor("#ffffff")
        leg.get_frame().set_edgecolor("#cccccc")

    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    return _save_to_png(fig)


def _save_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
