# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""GPU 描画の試作アプリ。

ECMWF の 2m 気温と 10m 風（日本の周り、0〜72 時間）を、自作の部品 GpuField で描く。
拡大・移動・風の粒子の動きは部品の中（Flutter・GPU）で完結し、Python は時刻や層を
変えたときにだけデータを渡す。データは prepare_data.py が作ったもの（src/data/ に置く）。
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import flet as ft
from flet_gpufield import GpuField, GpuFieldStatsEvent

RUN = "2026101212"
DATA = Path(__file__).resolve().parent / "data" / RUN
META = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
STEPS: list[int] = META["steps"]
LAYERS: dict = META["layers"]
COAST = (DATA / "coast.bin").read_bytes()
LUTS = {key: (DATA / spec["lut"]).read_bytes() for key, spec in LAYERS.items()}
RUN_TIME = datetime.strptime(RUN, "%Y%m%d%H").replace(tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))
STATS_LOG = os.environ.get("GPUVIEW_STATS")
# 測定用: 粒子の数（0 で粒子なし）と海岸線の有無を環境変数で変える
N_PARTICLES = int(os.environ.get("GPUVIEW_PARTICLES", "10000"))
SHOW_COAST = os.environ.get("GPUVIEW_COAST", "1") != "0"
TRAIL = os.environ.get("GPUVIEW_TRAIL", "vertices")
# 測定用: 時刻を自動で進める間隔（秒）。指定すると最初から再生する
PLAY_EVERY = float(os.environ.get("GPUVIEW_PLAY", "0") or 0)


def load_step(step: int) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    data = (DATA / f"{step:03d}.bin").read_bytes()
    return data, (time.perf_counter() - t0) * 1000


def valid_label(step: int) -> str:
    valid = RUN_TIME + timedelta(hours=step)
    return (f"T+{step}h  {valid:%m/%d %H}UTC（{valid.astimezone(JST):%m/%d %H}時 JST）")


@ft.component
def App():
    index, set_index = ft.use_state(0)
    layer, set_layer = ft.use_state("t2m")
    playing, set_playing = ft.use_state(PLAY_EVERY > 0)
    particles, set_particles = ft.use_state(N_PARTICLES > 0)
    stats, set_stats = ft.use_state("")

    step = STEPS[index]
    field, load_ms = ft.use_memo(lambda: load_step(step), [step])

    def start_playback():
        if not playing:
            return None

        async def advance():
            while True:
                await asyncio.sleep(PLAY_EVERY or 0.5)
                set_index(lambda i: (i + 1) % len(STEPS))

        task = asyncio.get_running_loop().create_task(advance())
        return task.cancel

    ft.use_effect(start_playback, [playing])

    def on_stats(e: GpuFieldStatsEvent):
        line = (f"{e.fps:.0f} fps / 粒子 {e.particles} / 粒子の計算 {e.frame_ms:.2f} ms / "
                f"build {e.build_ms:.1f} ms / raster {e.raster_ms:.1f} ms / "
                f"GPU 用の画像を作る {e.upload_ms:.1f} ms / ファイルを読む {load_ms:.1f} ms")
        set_stats(line)
        if STATS_LOG:   # 画面を見られないときの測定用（GPUVIEW_STATS=ファイル）
            with open(STATS_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} trail={TRAIL} layer={layer} step={step} {line}\n")

    spec = LAYERS[layer]
    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=6,
            controls=[
                ft.Row(
                    controls=[
                        ft.SegmentedButton(
                            segments=[ft.Segment(value=key, label=ft.Text(s["short"]))
                                      for key, s in LAYERS.items()],
                            selected=[layer],
                            on_change=lambda e: set_layer(e.control.selected[0]),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.PAUSE if playing else ft.Icons.PLAY_ARROW,
                            tooltip="時刻を自動で進める",
                            on_click=lambda: set_playing(not playing),
                        ),
                        ft.Slider(
                            min=0, max=len(STEPS) - 1, divisions=len(STEPS) - 1,
                            value=index, expand=True,
                            on_change=lambda e: set_index(int(e.control.value)),
                        ),
                        ft.Text(valid_label(step), width=260),
                        ft.Switch(label="風の粒子", value=particles,
                                  on_change=lambda e: set_particles(e.control.value)),
                    ],
                ),
                GpuField(
                    expand=True,
                    field=field,
                    nx=META["nx"], ny=META["ny"],
                    lon0=META["lon0"], lat0=META["lat0"],
                    dlon=META["dlon"], dlat=META["dlat"],
                    layer=layer, vmin=spec["vmin"], vmax=spec["vmax"], lut=LUTS[layer],
                    coast=COAST if SHOW_COAST else None,
                    particles=max(N_PARTICLES, 0),
                    trail=TRAIL,
                    show_particles=particles,
                    attribution=f"ECMWF IFS {META['run']}  {META['attribution']}",
                    on_stats=on_stats,
                ),
                ft.Text(stats, size=12),
            ],
        ),
    )


def _configure(page: ft.Page) -> None:
    page.title = "GPU 描画の試作（AIseed Weather）"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 8


ft.run(lambda page: page.render(App), before_main=_configure)
