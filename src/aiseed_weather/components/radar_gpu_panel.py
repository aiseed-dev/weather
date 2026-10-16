# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""動く地図（GPU 描画）でのナウキャスト。ナウキャストのタブの「動く地図」で、図の代わりに出す。

レーダーの強さの階級を flet-gpufield に渡し、拡大・移動・時刻の切り替えは部品の中で行う。
コマのデータは RadarView が取得して渡す（**この部品は取得しない**）。階級なので補間せず、
最寄りの格子点の値で塗る（sampling="nearest"）。

部品の入った Flet の実行環境が要る（tools/build_client.sh）。ready が届かなければ作り方を案内する。
"""

from __future__ import annotations

import asyncio
import sys

import flet as ft

from aiseed_weather.components.task_effect import use_task_effect
from aiseed_weather.figures._basemap import SEA_RGB
from aiseed_weather.figures._japan_map import JMA_PRECIP_COLOURS, JMA_PRECIP_LABELS, area
from aiseed_weather.figures.radar_map import NO_DATA_RGBA
from aiseed_weather.services import gpu_radar, jma_endpoints

# 部品の ready を待つ時間。Web 版で 0.3〜1.3 秒（2026-10-14）だが、最初のコマの受け渡しが
# 重いと 20 秒近く遅れたことがある（2026-10-13、Web 版）。案内は ready が届けば消える。
READY_TIMEOUT_SEC = 10.0
# 部品入りの実行環境の作り方。Linux は tools/build_client.sh（bash）、Windows は client で flet build
CLIENT_HOWTO = ("一度だけ client のフォルダーで flet build windows -o ..\\build\\windows を実行し、\n"
                if sys.platform == "win32" else
                "一度だけ tools/build_client.sh を実行し（build/linux に作られます）、\n")


def _message(text: str, *, icon=ft.Icons.INFO_OUTLINE) -> ft.Control:
    return ft.Container(
        alignment=ft.Alignment.CENTER,
        padding=24,
        content=ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
            controls=[ft.Icon(icon, size=36, color=ft.Colors.GREY),
                      ft.Text(text, color=ft.Colors.GREY, text_align=ft.TextAlign.CENTER)],
        ),
    )


def _veiled_sea() -> str:
    """The no-data veil over the sea colour, as the map shows it."""
    r, g, b, a = NO_DATA_RGBA
    mixed = [int(round(float(base) * (1 - a) + c * 255 * a)) for base, c in zip(SEA_RGB, (r, g, b))]
    return "#%02x%02x%02x" % tuple(mixed)


def _chip(colour: str, label: str, width: int = 46) -> ft.Control:
    return ft.Column(spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
        ft.Container(width=width, height=10, bgcolor=colour,
                     border=ft.Border.all(width=0.5, color=ft.Colors.BLACK_26)),
        ft.Text(label, size=10, color=ft.Colors.GREY),
    ])


def _legend() -> ft.Control:
    return ft.Row(spacing=2, wrap=True, vertical_alignment=ft.CrossAxisAlignment.START, controls=[
        ft.Text("降水強度（mm/h）", size=11, color=ft.Colors.GREY),
        *[_chip(colour, label) for colour, label in zip(JMA_PRECIP_COLOURS, JMA_PRECIP_LABELS)],
        ft.Container(width=12),
        _chip(_veiled_sea(), "観測範囲外・欠測", width=60),
    ])


@ft.component
def RadarGpuPanel(area_key: str, frames: tuple, step: int, status_text: str):
    """frames は (番号, 鍵, データ) の並び。step は描く番号。"""
    try:
        from flet_gpufield import GpuField, GpuFrame
    except ImportError:
        return _message("動く地図の部品（flet-gpufield）が入っていません。\n"
                        "pip install -e packages/flet-gpufield を実行してください。",
                        icon=ft.Icons.ERROR_OUTLINE)

    grid = ft.use_memo(lambda: gpu_radar.area_grid(area(area_key)), [area_key])
    static = ft.use_memo(lambda: {"lut": gpu_radar.lut_rgba(), "base": gpu_radar.land_mask(grid),
                                  "coast": gpu_radar.coastline()}, [area_key])
    ready, set_ready = ft.use_state(False)
    timed_out, set_timed_out = ft.use_state(False)

    async def wait_for_ready():
        await asyncio.sleep(READY_TIMEOUT_SEC)
        set_timed_out(True)

    use_task_effect(wait_for_ready, [])

    lon_min, lon_max, lat_min, lat_max = grid.extent
    gpu = GpuField(
        expand=True,
        nx=grid.nx, ny=grid.ny, lon0=grid.lon0, lat0=grid.lat0, dlon=grid.dlon, dlat=grid.dlat,
        projection="plate_carree",
        center_lon=(lon_min + lon_max) / 2, center_lat=(lat_min + lat_max) / 2,
        px_per_deg=1100.0 / (lon_max - lon_min),
        vmin=0.0, vmax=float(gpu_radar.N_LEVELS), lut=static["lut"],
        base=static["base"], coast=static["coast"],
        sampling="nearest", classes=list(gpu_radar.CLASS_LABELS),
        frames=[GpuFrame(key=key, index=i, scalar8=data) for i, key, data in frames],
        step=step,
        particles=0, show_particles=False,
        attribution=jma_endpoints.ATTRIBUTION_PROCESSED_INLINE,
        status_text=status_text,
        on_ready=lambda: set_ready(True),
    )
    overlay = []
    if timed_out and not ready:
        overlay.append(ft.Container(
            left=0, right=0, top=0, bottom=0,
            bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.SURFACE),
            content=_message(
                "動く地図の部品から応答がありません（応答が届けば、この案内は消えます）。\n"
                "消えないときは、いまの Flet の実行環境に部品が入っていません。\n"
                + CLIENT_HOWTO
                + "プロジェクトのフォルダーから flet run で起動し直してください。",
                icon=ft.Icons.BUILD_CIRCLE_OUTLINED),
        ))
    return ft.Column(expand=True, spacing=6, controls=[
        # 部品が無いと Flet は細い「Unknown control」の箱を出す。案内が細くならないよう枠いっぱいに
        ft.Stack(expand=True, fit=ft.StackFit.EXPAND, controls=[gpu, *overlay]),
        _legend(),
    ])
