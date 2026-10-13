# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""降水ナウキャスト / Nowcast — JMA's high-resolution precipitation nowcast.

Stays idle on mount; fetches only when the user presses 取得 / Fetch, picks
another time, or switches 図 / 動く地図 (parameter changes — see the
`user-action-fetch` skill). JMA is asked for at most four tiles a second.

The area — the whole country or a region (``_japan_map.AREAS``) — is
shared by both modes; each has its own tile zoom:

- 図: one frame as the exportable figure; zoom 6 (~2 km, 36 tiles) for the
  whole country, zoom 8 (~0.5 km, 30 – 56 tiles) for a region — 8 to 14
  seconds for a frame not on disk yet.
- 動く地図: every frame of the past hour (or three) and the hour ahead on
  the GPU (``radar_gpu_panel``); zoom 4 (~8 km, the tiles JMA's nationwide
  animation uses) for the whole country, zoom 6 (~2 km) for a region,
  4 – 9 tiles a frame. Frames appear as they arrive, the latest
  observation first; play steps through them.

Where JMA has no data (outside the radars' range, or a radar down) both
modes veil the map, so "not observed" never reads as "no rain".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, timedelta

import flet as ft

from aiseed_weather.components.observation_common import (
    JST,
    Result,
    ResultArea,
    StatusArea,
    dropdown,
    fetch_buttons,
)
from aiseed_weather.components.radar_gpu_panel import RadarGpuPanel
from aiseed_weather.figures._japan_map import AREAS, JapanArea, area, tile_km
from aiseed_weather.figures.observation_chart import Provenance
from aiseed_weather.figures.radar_map import render_radar_map
from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services import gpu_radar, jma_endpoints
from aiseed_weather.services.jma_radar_service import (
    JmaRadarService,
    RadarFrame,
    RadarMosaic,
    RadarTimes,
)

logger = logging.getLogger(__name__)

SCREEN_DPI = 100
PLAY_INTERVAL_SEC = 0.5
PAST_HOURS = (("1", "過去 1 時間"), ("3", "過去 3 時間"))


@dataclass(frozen=True, eq=False)
class RadarFigure:
    """What the radar figure needs, kept so PNG export can re-render it."""

    mosaic: RadarMosaic
    area: JapanArea
    file_stem: str
    prov: Provenance

    def render(self, dpi: int) -> bytes:
        m, a = self.mosaic, self.area
        note = (f"ズーム {m.zoom} のタイル（約 {_km(m.zoom, a)} km）を、"
                "図の画素ごとに最も強い値で表示。")
        if m.missing:
            note += f"{m.missing}/{m.tiles} 枚のタイルが取得できず、その範囲は空白。"
        return render_radar_map(m.classes, m.lons(), m.lats(), valid=m.frame.valid,
                                base=m.frame.base, attribution=self.prov.license_text,
                                extent=a.extent, area_name="" if a is AREAS[0] else a.name,
                                no_data=m.no_data, note=note, dpi=dpi)


def _km(zoom: int, a: JapanArea) -> str:
    km = tile_km(zoom, a.mid_lat)
    return f"{km:.0f}" if km >= 1 else f"{km:.1f}"


def _area_label(a: JapanArea, mode: str) -> str:
    return f"{a.name}（約 {_km(a.figure_zoom if mode == 'figure' else a.moving_zoom, a)} km）"


def _frame_label(f: RadarFrame) -> str:
    jst = f.valid.astimezone(JST)
    return f"{jst:%H:%M} 予測 +{f.lead_minutes} 分" if f.is_forecast else f"{jst:%H:%M} 観測"


def _valid_line(f: RadarFrame) -> str:
    valid = f"{f.valid.astimezone(JST):%Y-%m-%d %H:%M} JST（{f.valid.astimezone(UTC):%H:%M} UTC）"
    if f.is_forecast:
        return f"予測 +{f.lead_minutes} 分 {valid} · 初期時刻 {f.base.astimezone(JST):%H:%M} JST"
    return f"観測 {valid}"


def _fetched_line(f: RadarFrame, times: RadarTimes) -> str:
    return (f"{_valid_line(f)} · 時刻一覧の取得 {times.fetched_at.astimezone(JST):%H:%M} JST"
            f" · {jma_endpoints.ATTRIBUTION}")


def _moving_frames(times: RadarTimes, past_hours: int) -> list[RadarFrame]:
    """The moving map's frames by time: observations of the past hours, then the forecast."""
    start = times.latest_observation.valid - timedelta(hours=past_hours)
    return [f for f in times.frames if f.is_forecast or f.valid >= start]


def _load_order(frames: list[RadarFrame]) -> list[RadarFrame]:
    """Latest observation, the forecast forward, then the past backward."""
    obs = [f for f in frames if not f.is_forecast]
    forecast = [f for f in frames if f.is_forecast]
    return obs[-1:] + forecast + obs[-2::-1]


@ft.component
def RadarView(settings: UserSettings):
    state, set_state = ft.use_state("idle")        # idle | loading | ready | error
    progress, set_progress = ft.use_state("")
    error, set_error = ft.use_state(None)
    times, set_times = ft.use_state(None)          # RadarTimes
    frame_key, set_frame_key = ft.use_state(None)
    result, set_result = ft.use_state(None)
    mode, set_mode = ft.use_state("figure")        # figure | gpu
    past_hours, set_past_hours = ft.use_state(1)
    area_key, set_area_key = ft.use_state(AREAS[0].key)
    packed, set_packed = ft.use_state({})          # moving map: "area|frame key" → field bytes
    failed, set_failed = ft.use_state(frozenset())
    gpu_loading, set_gpu_loading = ft.use_state(False)
    playing, set_playing = ft.use_state(False)
    # One service for the view's lifetime: it paces and serialises tile fetches.
    service = ft.use_memo(lambda: JmaRadarService(data_dir=resolved_data_dir(settings)), [])

    async def show(frame: RadarFrame, t: RadarTimes, a: JapanArea):
        def on_progress(done: int, total: int):
            set_progress(f"Fetching radar tiles from JMA… {done}/{total}")

        mosaic = await service.mosaic(frame, a.extent, a.figure_zoom, on_progress=on_progress)
        set_progress("Rendering radar map…")
        jst = frame.valid.astimezone(JST)
        stem = f"nowcast_{a.key}_{jst:%Y%m%d%H%M}" + (f"_base{frame.base.astimezone(JST):%H%M}"
                                                       if frame.is_forecast else "")
        figure = RadarFigure(
            mosaic=mosaic, area=a, file_stem=stem,
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
            if mode == "figure":
                await show(frame, t, area(area_key))
            set_state("ready")
        except Exception as e:
            logger.exception("Radar view failed to fetch the JMA nowcast")
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    async def show_figure(key: str, a: JapanArea):
        frame = times.find(key) if times else None
        if frame is None:
            return
        set_state("loading")
        set_error(None)
        try:
            await show(frame, times, a)
            set_state("ready")
        except Exception as e:
            logger.exception("Radar view failed to fetch frame %s", key)
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    # ── moving map: fetch every frame once, newest first ──────────────

    moving = _moving_frames(times, past_hours) if times else []
    times_key = times.fetched_at.isoformat() if times else ""

    current_area = area(area_key)

    def packed_key(f: RadarFrame) -> str:
        return f"{current_area.key}|{f.key}"

    def start_moving_map():
        if mode != "gpu" or times is None:
            return None
        wanted = {packed_key(f) for f in moving}
        set_packed(lambda cur: {k: v for k, v in cur.items() if k in wanted})
        set_failed(frozenset())
        a = current_area
        grid = gpu_radar.area_grid(a)
        order = [f for f in _load_order(moving) if packed_key(f) not in packed]

        async def fetch_frames():
            set_gpu_loading(True)
            try:
                for f in order:
                    try:
                        m = await service.mosaic(f, a.extent, a.moving_zoom)
                        data = await asyncio.to_thread(gpu_radar.pack_frame, m.classes,
                                                       m.lons(), m.lats(), grid, m.no_data)
                    except Exception:
                        logger.exception("Moving-map nowcast frame %s failed", f.key)
                        set_failed(lambda cur, k=f.key: cur | {k})
                        continue
                    set_packed(lambda cur, k=packed_key(f), d=data: {**cur, k: d})
            finally:
                set_gpu_loading(False)

        task = asyncio.get_running_loop().create_task(fetch_frames())
        return task.cancel

    ft.use_effect(start_moving_map, [mode, times_key, past_hours, area_key])

    loaded = [f.key for f in moving if packed_key(f) in packed]

    def start_playing():
        if not playing or mode != "gpu" or len(loaded) < 2:
            return None

        async def advance():
            while True:
                await asyncio.sleep(PLAY_INTERVAL_SEC)
                set_frame_key(lambda cur: loaded[(loaded.index(cur) + 1) % len(loaded)]
                              if cur in loaded else loaded[0])

        task = asyncio.get_running_loop().create_task(advance())
        return task.cancel

    ft.use_effect(start_playing, [playing, mode, len(loaded)])

    # ── controls ──────────────────────────────────────────────────────

    frames = moving if mode == "gpu" else (list(times.frames) if times else [])
    keys = [f.key for f in frames]
    at = keys.index(frame_key) if frame_key in keys else -1
    busy = state == "loading"

    def go_to(key: str):
        set_playing(False)
        set_frame_key(key)
        if mode == "figure":
            ft.context.page.run_task(show_figure, key, current_area)

    def on_mode(value: str):
        set_mode(value)
        set_playing(False)
        if value == "figure" and times is not None and frame_key:
            ft.context.page.run_task(show_figure, frame_key, current_area)

    def on_area(key: str):
        set_area_key(key)
        if mode == "figure" and times is not None and frame_key:
            ft.context.page.run_task(show_figure, frame_key, area(key))

    def on_past_hours(value: str):
        set_past_hours(int(value))
        if times and frame_key not in [f.key for f in _moving_frames(times, int(value))]:
            set_frame_key(times.latest_observation.key)

    time_controls = [
        dropdown("時刻（JST）", frame_key or "", [(f.key, _frame_label(f)) for f in frames],
                 go_to, width=210),
        ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="5 分前",
                      disabled=busy or at <= 0, on_click=lambda _: go_to(keys[at - 1])),
        *([ft.IconButton(ft.Icons.PAUSE if playing else ft.Icons.PLAY_ARROW,
                         tooltip="止める" if playing else "再生",
                         disabled=len(loaded) < 2,
                         on_click=lambda _: set_playing(not playing))] if mode == "gpu" else []),
        ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="5 分後",
                      disabled=busy or not 0 <= at < len(keys) - 1,
                      on_click=lambda _: go_to(keys[at + 1])),
    ] if frames else []
    mode_row = ft.Row(controls=[
        ft.SegmentedButton(
            segments=[ft.Segment(value="figure", label=ft.Text("図")),
                      ft.Segment(value="gpu", label=ft.Text("動く地図"))],
            selected=[mode],
            on_change=lambda e: on_mode(e.control.selected[0]),
        ),
    ])
    controls_row = ft.Row(wrap=True, spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                          controls=[
        dropdown("地域", area_key, [(a.key, _area_label(a, mode)) for a in AREAS], on_area,
                 width=230),
        *([dropdown("範囲", str(past_hours), PAST_HOURS, on_past_hours, width=160)]
          if mode == "gpu" else []),
        *time_controls,
        fetch_buttons(state, load),
    ])
    idle_text = ("気象庁の高解像度降水ナウキャストを、日本全体か地方の図にします。最新の観測のほか、"
                 "過去 3 時間（5 分ごと）と 1 時間先までの予測を選べます。"
                 if mode == "figure" else
                 "過去の観測と 1 時間先までの予測を、動く地図でコマ送り・再生します。"
                 "全国は約 8 km、地方は約 2 km のタイルで、1 コマ 4〜9 枚を取得します。")
    footer = ft.Text(f"{jma_endpoints.ATTRIBUTION}。高解像度降水ナウキャストの地図タイルを"
                     "取得して編集・加工しています。", size=10, color=ft.Colors.GREY)
    header = ft.Text("降水ナウキャスト / Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD)

    if mode == "gpu" and state == "ready" and times is not None:
        frame = times.find(frame_key) if frame_key else None
        if frame is not None and packed_key(frame) in packed:
            status_text = ""
        elif frame_key in failed:
            status_text = "この時刻は取得できませんでした"
        else:
            status_text = f"読み込み中…（{len(loaded)}/{len(moving)} コマ）"
        fetched = (f"{len(loaded)}/{len(moving)} コマ" + (" 取得中" if gpu_loading else "")
                   + (f" · {len(failed)} コマ取得できず" if failed else ""))
        body = ft.Column(expand=True, spacing=10, controls=[
            controls_row,
            ft.Text(f"{_valid_line(frame) if frame else ''} · {fetched} · "
                    "拡大はホイール、移動はドラッグ。マウスを置くと強さを読める",
                    size=11, color=ft.Colors.GREY),
            ft.Container(expand=True, content=RadarGpuPanel(
                area_key=current_area.key,
                frames=tuple((i, packed_key(f), packed[packed_key(f)])
                             for i, f in enumerate(moving) if packed_key(f) in packed),
                step=at if at >= 0 else 0,
                status_text=status_text,
            )),
            footer,
        ])
    else:
        body = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=12, controls=[
            controls_row,
            StatusArea(state, progress, error, idle_text),
            ResultArea(result) if mode == "figure" and state == "ready" and result
            else ft.Container(height=0),
            footer,
        ])
    # The header and the 図 / 動く地図 switch stay put while the figure scrolls.
    return ft.Container(padding=16, expand=True, content=ft.Column(
        expand=True, spacing=10, controls=[header, mode_row, body],
    ))
