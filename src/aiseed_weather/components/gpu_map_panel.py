# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""動く地図（GPU 描画）の部品。地図画面の「動く地図」で、画像の代わりに出す。

色の場・風の粒子・等値線を Flutter 側（flet-gpufield）が GPU で描く。拡大・移動・粒子の
動きは部品の中で完結し、Python は時刻ごとのデータを 1 回ずつ渡すだけ。時刻の切り替えは
番号（step）を変えるだけなので、再生しても描き直しの待ちが無い。

データはキャッシュ済みのファイル（bulk GRIB か配布パック）から作る。**この部品は取得しない**
（user-action-fetch）。無い時刻は飛ばし、取得は地図画面の「GPV データ」から行う。

部品の入った Flet の実行環境が要る（tools/build_client.sh で build/linux に作る）。
出来合いの実行環境だと部品が表示されないので、ready が届かなければ作り方を案内する。
"""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from aiseed_weather.components.task_effect import use_task_effect
from aiseed_weather.services import gpu_frames
from aiseed_weather.services.forecast_service import grib_cache_path, is_grib_cached

logger = logging.getLogger(__name__)

READY_TIMEOUT_SEC = 4.0


def _step_files(settings, cycle, step: int, kinds) -> dict | None:
    files = {}
    for kind in kinds:
        if not is_grib_cached(settings, cycle, step, kind=kind):
            return None
        files[kind] = grib_cache_path(settings, cycle, step, kind=kind)
    return files


def _static_for(domain: gpu_frames.GpuDomain, layer: gpu_frames.GpuLayer) -> dict:
    lut, vmin, vmax = gpu_frames.lut_rgba(layer)
    return {
        "lut": lut, "vmin": vmin, "vmax": vmax,
        "base": gpu_frames.land_mask(domain),
        "coast": gpu_frames.coastline(domain),
    }


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


@ft.component
def GpuMapPanel(settings, plan: tuple, region, layer_key: str, step_hours: int,
                msl_overlay: bool, attribution: str):
    """plan は地図画面と同じ (表示の時刻, 元の周期, 元の時刻) の並び。"""
    try:
        from flet_gpufield import GpuField, GpuFrame
    except ImportError:
        return _message("動く地図の部品（flet-gpufield）が入っていません。\n"
                        "pip install -e packages/flet-gpufield を実行してください。",
                        icon=ft.Icons.ERROR_OUTLINE)

    layer = ft.use_memo(lambda: gpu_frames.layer_for(layer_key), [layer_key])
    domain = ft.use_memo(lambda: gpu_frames.domain_for(region), [region.key, region.extent])
    static = ft.use_memo(
        lambda: _static_for(domain, layer) if layer is not None else None,
        [region.key, region.extent, layer_key],
    )
    frames, set_frames = ft.use_state({})
    finished, set_finished = ft.use_state(False)
    missing, set_missing = ft.use_state(frozenset())   # 手元にファイルが無い時刻（番号）
    ready, set_ready = ft.use_state(False)
    timed_out, set_timed_out = ft.use_state(False)
    stats, set_stats = ft.use_state("")

    plan_key = tuple((d, c.isoformat(), s) for d, c, s in plan)
    try:
        step_index = [p[0] for p in plan].index(step_hours)
    except ValueError:
        step_index = 0
    use_overlay = msl_overlay and layer_key != "msl"
    load_key = f"{plan_key[0][1] if plan_key else ''}|{region.key}|{layer_key}|{int(use_overlay)}"

    def start_loading():
        if layer is None or not plan:
            return None
        set_frames({})
        set_finished(False)
        set_missing(frozenset())
        builder = gpu_frames.FrameBuilder(layer, domain, msl_overlay=use_overlay)
        kinds = gpu_frames.kinds_for(layer, use_overlay)
        entries = list(plan)
        first = step_index

        async def load():
            # 今の時刻から近い順に作って渡す（再生を始めてもすぐ動く）
            for i in sorted(range(len(entries)), key=lambda k: abs(k - first)):
                _, cycle, step = entries[i]
                files = _step_files(settings, cycle, step, kinds)
                if files is None:
                    set_missing(lambda cur, i=i: cur | {i})
                    continue
                prev = None
                if layer.interval_precip:
                    for j in range(i - 1, -1, -1):
                        if entries[j][1] == cycle:
                            prev = _step_files(settings, cycle, entries[j][2], kinds)
                            break
                try:
                    frame = await asyncio.to_thread(builder.build, files, prev)
                except Exception:
                    logger.exception("GPU frame build failed: %s +%dh", cycle, step)
                    continue
                logger.debug("GPU frame %d (+%dh) built in %.2fs", i, step, frame.seconds)
                set_frames(lambda cur, i=i, frame=frame: {**cur, i: frame})
            set_finished(True)

        return load()

    use_task_effect(start_loading, [load_key, plan_key])

    async def wait_for_ready():
        await asyncio.sleep(READY_TIMEOUT_SEC)
        set_timed_out(True)

    use_task_effect(wait_for_ready, [])

    if layer is None:
        return _message("この層は動く地図ではまだ描けません。「図」に切り替えてください。")
    if finished and not frames:
        return _message("この周期のデータがまだ手元にありません。\n"
                        "左の「GPV データ」から取得してください（動く地図は取得しません）。")

    def on_stats(e):
        logger.debug("GPU view: %.0f fps, build %.1f ms, raster %.1f ms, particles %.1f ms, %d frames",
                     e.fps, e.build_ms, e.raster_ms, e.frame_ms, e.frames)
        set_stats(f"{e.fps:.0f} fps · 粒子 {e.particles:,} · 時刻 {e.frames}/{len(plan)}")

    if step_index in frames:
        status_text = ""
    elif step_index in missing:
        status_text = "この時刻のデータは手元にありません（前の時刻を表示中）"
    else:
        status_text = f"読み込み中…（{len(frames)}/{len(plan)} 時刻）"
    gpu = GpuField(
        expand=True,
        nx=domain.nx, ny=domain.ny, lon0=domain.lon0, lat0=domain.lat0,
        dlon=domain.dlon, dlat=domain.dlat, periodic=domain.periodic,
        projection=domain.projection,
        center_lon=domain.center_lon, center_lat=domain.center_lat,
        px_per_deg=domain.px_per_deg,
        vmin=static["vmin"], vmax=static["vmax"], lut=static["lut"],
        base=static["base"], coast=static["coast"],
        label=layer.label, unit=layer.unit,
        frames=[
            GpuFrame(key=f"{load_key}:{i}", index=i, scalar=f.scalar, wind=f.wind,
                     contours=f.contours, contours_bold=f.contours_bold, labels=f.labels)
            for i, f in sorted(frames.items())
        ],
        step=step_index,
        attribution=attribution,
        status_text=status_text,
        on_ready=lambda: set_ready(True),
        on_stats=on_stats,
    )
    overlay = []
    if timed_out and not ready:
        overlay.append(ft.Container(
            left=0, right=0, top=0, bottom=0,
            bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.SURFACE),
            content=_message(
                "動く地図の部品が、いまの Flet の実行環境に入っていません。\n"
                "一度だけ tools/build_client.sh を実行し（build/linux に作られます）、\n"
                "プロジェクトのフォルダーから flet run で起動し直してください。",
                icon=ft.Icons.BUILD_CIRCLE_OUTLINED),
        ))
    loading = "" if finished else f"読み込み中 {len(frames)}/{len(plan)} 時刻 · "
    return ft.Column(
        expand=True,
        spacing=2,
        controls=[
            ft.Stack(expand=True, controls=[gpu, *overlay]),
            ft.Text(f"{loading}{stats}", size=11, color=ft.Colors.GREY),
        ],
    )
