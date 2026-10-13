# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""降水ナウキャスト / Nowcast — JMA's high-resolution precipitation nowcast.

Stays idle on mount; fetches only when the user presses 取得 / Fetch or
picks another time (a parameter change — see the `user-action-fetch`
skill). A frame is the 36 tiles covering Japan at zoom 6 (~2 km). JMA is
asked for at most four tiles a second, so a frame not on disk yet takes
about nine seconds; one already fetched shows at once.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC

import flet as ft

from aiseed_weather.components.observation_common import (
    JST,
    Result,
    ResultArea,
    StatusArea,
    dropdown,
    fetch_buttons,
)
from aiseed_weather.figures._japan_map import EXTENT
from aiseed_weather.figures.observation_chart import Provenance
from aiseed_weather.figures.radar_map import render_radar_map
from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services import jma_endpoints
from aiseed_weather.services.jma_radar_service import (
    JmaRadarService,
    RadarFrame,
    RadarMosaic,
    RadarTimes,
)

logger = logging.getLogger(__name__)

ZOOM = 6
SCREEN_DPI = 100


@dataclass(frozen=True, eq=False)
class RadarFigure:
    """What the radar figure needs, kept so PNG export can re-render it."""

    mosaic: RadarMosaic
    file_stem: str
    prov: Provenance

    def render(self, dpi: int) -> bytes:
        m = self.mosaic
        note = "ズーム 6 のタイル（約 2 km）を、図の画素ごとに最も強い値で表示。"
        if m.missing:
            note += f"{m.missing}/{m.tiles} 枚のタイルが取得できず、その範囲は空白。"
        return render_radar_map(m.classes, m.lons(), m.lats(), valid=m.frame.valid,
                                base=m.frame.base, attribution=self.prov.license_text,
                                note=note, dpi=dpi)


def _frame_label(f: RadarFrame) -> str:
    jst = f.valid.astimezone(JST)
    return f"{jst:%H:%M} 予測 +{f.lead_minutes} 分" if f.is_forecast else f"{jst:%H:%M} 観測"


def _fetched_line(f: RadarFrame, times: RadarTimes) -> str:
    valid = f"{f.valid.astimezone(JST):%Y-%m-%d %H:%M} JST（{f.valid.astimezone(UTC):%H:%M} UTC）"
    head = (f"予測 +{f.lead_minutes} 分 {valid} · 初期時刻 {f.base.astimezone(JST):%H:%M} JST"
            if f.is_forecast else f"観測 {valid}")
    return (f"{head} · 時刻一覧の取得 {times.fetched_at.astimezone(JST):%H:%M} JST"
            f" · {jma_endpoints.ATTRIBUTION}")


@ft.component
def RadarView(settings: UserSettings):
    state, set_state = ft.use_state("idle")        # idle | loading | ready | error
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    times, set_times = ft.use_state(None)          # RadarTimes
    frame_key, set_frame_key = ft.use_state(None)
    result, set_result = ft.use_state(None)
    # One service for the view's lifetime: it serialises tile fetches.
    service = ft.use_memo(lambda: JmaRadarService(data_dir=resolved_data_dir(settings)), [])

    async def show(frame: RadarFrame, t: RadarTimes):
        def on_progress(done: int, total: int):
            set_progress(f"Fetching radar tiles from JMA… {done}/{total}")

        mosaic = await service.mosaic(frame, EXTENT, ZOOM, on_progress=on_progress)
        set_progress("Rendering radar map…")
        jst = frame.valid.astimezone(JST)
        stem = f"nowcast_{jst:%Y%m%d%H%M}" + (f"_base{frame.base.astimezone(JST):%H%M}"
                                              if frame.is_forecast else "")
        figure = RadarFigure(
            mosaic=mosaic, file_stem=stem,
            prov=Provenance(data_source="JMA 高解像度降水ナウキャスト",
                            period_id=f"{jst:%Y-%m-%d %H:%M} JST",
                            license_text=jma_endpoints.ATTRIBUTION_PROCESSED_INLINE),
        )
        png = await asyncio.to_thread(figure.render, SCREEN_DPI)
        set_result(Result(spec=figure, png=png, header=(), rows=(),
                          fetched_line=_fetched_line(frame, t)))

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        try:
            set_progress("Fetching the nowcast time index from JMA…")
            t = await service.times(force=force)
            frame = t.latest_observation
            set_times(t)
            set_frame_key(frame.key)
            await show(frame, t)
            set_state("ready")
        except Exception as e:
            logger.exception("Radar view failed to fetch the JMA nowcast")
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    async def pick(key: str):
        frame = times.find(key) if times else None
        if frame is None:
            return
        set_frame_key(key)
        set_state("loading")
        set_error(None)
        try:
            await show(frame, times)
            set_state("ready")
        except Exception as e:
            logger.exception("Radar view failed to fetch frame %s", key)
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    frames = times.frames if times else ()
    keys = [f.key for f in frames]
    at = keys.index(frame_key) if frame_key in keys else -1
    busy = state == "loading"

    def step(delta: int):
        if 0 <= at + delta < len(keys):
            ft.context.page.run_task(pick, keys[at + delta])

    time_controls = [
        dropdown("時刻（JST）", frame_key or "", [(f.key, _frame_label(f)) for f in frames],
                 lambda k: ft.context.page.run_task(pick, k), width=190),
        ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="5 分前",
                      disabled=busy or at <= 0, on_click=lambda _: step(-1)),
        ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="5 分後",
                      disabled=busy or not 0 <= at < len(keys) - 1, on_click=lambda _: step(1)),
    ] if frames else []

    return ft.Container(padding=16, content=ft.Column(
        scroll=ft.ScrollMode.AUTO, expand=True, spacing=12, controls=[
            ft.Text("降水ナウキャスト / Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
            ft.Row(wrap=True, spacing=10, controls=[*time_controls, fetch_buttons(state, load)]),
            StatusArea(state, progress, error,
                       "気象庁の高解像度降水ナウキャストを日本全体の図にします。最新の観測のほか、"
                       "過去 3 時間（5 分ごと）と 1 時間先までの予測を選べます。"),
            ResultArea(result) if state == "ready" and result else ft.Container(height=0),
            ft.Text(f"{jma_endpoints.ATTRIBUTION}。高解像度降水ナウキャストの地図タイルを"
                    "取得して編集・加工しています。",
                    size=10, color=ft.Colors.GREY),
        ],
    ))
