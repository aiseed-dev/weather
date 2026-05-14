# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Map view: ECMWF synoptic chart with timeline animation.

Pipeline (only after the user presses 取得 / Fetch):
  probe latest run via ForecastService.latest_run → download GRIB2 →
  decode via cfgrib → render matplotlib figure → embed as PNG bytes.

Animation:
- After the first single-step fetch, the user can press
  ▶ アニメーション to pre-load every step in STEP_OPTIONS.
- Each rendered PNG is cached in component state under its step_hours
  key, so flipping back and forth or playing is instant after the
  initial pre-load.
- Standard timeline controls (⏮ / ▶⏸ / ⏭ + slider) match the JMA
  rainfall nowcast player UX.
- Auto-fetch on step change is the user-action-fetch skill's
  "parameter change = user action" exception.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

import flet as ft
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from aiseed_weather.figures.msl_chart import render_msl
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.services.forecast_service import (
    ForecastDisabledError,
    ForecastRequest,
    ForecastService,
)

logger = logging.getLogger(__name__)


# HRES 0p25 oper publishes step=0..144 every 3h and step=150..240 every 6h.
# The full 3h cadence (65 frames) makes a smooth synoptic animation but
# is heavy to preload — figure roughly 10 min cold, 5 min cache-warm for
# all frames on a typical laptop.
STEP_OPTIONS = tuple(list(range(0, 145, 3)) + list(range(150, 241, 6)))
FRAME_INTERVAL_SEC = 0.9  # animation playback frame duration
# Spacing between consecutive S3 fetches during preload. S3's per-prefix
# rate limit triggers "503 Slow Down" if we hammer one bucket prefix; a
# small pause between requests keeps the rate below that threshold and
# spares us from multi-second retry backoffs.
PRELOAD_SPACING_SEC = 0.5


def _step_label(h: int) -> str:
    if h == 0:
        return "T+0h (analysis)"
    days = h // 24
    rest = h % 24
    return f"T+{h}h (D+{days})" if rest == 0 else f"T+{h}h"


def _figure_to_png_bytes(fig: Figure, *, dpi: int = 120) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


@ft.component
def MapView(settings: UserSettings):
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error | disabled
    image_bytes, set_image_bytes = ft.use_state(None)
    error, set_error = ft.use_state(None)
    run_label, set_run_label = ft.use_state("")
    progress, set_progress = ft.use_state("")
    step_hours, set_step_hours = ft.use_state(0)

    # Animation state.
    # frames maps step_hours -> rendered PNG bytes for that frame.
    # run_time_holder remembers the IFS cycle the frames belong to;
    # when we need to load more frames we reuse the same cycle so the
    # animation is consistent.
    frames, set_frames = ft.use_state({})
    is_playing, set_is_playing = ft.use_state(False)
    run_time_holder, set_run_time_holder = ft.use_state(None)
    # Stable mutable holder for the currently-scheduled animation task.
    # ft.use_state with a lazy initializer returns the same dict object
    # across renders, so setup can write the task reference and cleanup
    # can read it back even though both closures are recreated every
    # render.
    anim_task_ref, _ = ft.use_state(lambda: {"task": None})

    async def _render_step(service, run_time, step: int) -> bytes:
        request = ForecastRequest(run_time=run_time, step_hours=step, param="msl")
        ds = await service.fetch(request)
        label = f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
        fig = await asyncio.to_thread(
            render_msl, ds, projection="robinson", run_id=label,
        )
        return await asyncio.to_thread(_figure_to_png_bytes, fig)

    MAX_STEP = max(STEP_OPTIONS)

    async def _resolve_run_time(service, *, force: bool):
        """Pick the IFS cycle every frame must come from.

        Probes with the LARGEST step so we always lock onto a run that has
        published its full forecast horizon. Probing with a smaller step
        would give the absolute latest cycle, but that cycle may not yet
        have its long-range fields out — leading to 404s deep into the
        animation preload. The trade-off is that the frame at T+0 is a
        few hours older than the strictly-newest analysis.

        Reuses the cached holder unless force=True (Refresh button) or no
        cycle has been resolved yet.
        """
        if run_time_holder is not None and not force:
            return run_time_holder, False  # (run_time, did_probe)
        set_progress(
            f"Probing latest fully-published ECMWF run (need T+{MAX_STEP}h)…"
        )
        new_run = await service.latest_run(step_hours=MAX_STEP, param="msl")
        return new_run, True

    async def load(*, step: int, force: bool = False):
        """Fetch + render a single step. Caches the resulting PNG."""
        set_state("loading")
        set_error(None)
        set_progress("Initializing forecast service…")
        try:
            service = ForecastService(settings)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            return

        try:
            t0 = time.perf_counter()
            run_time, did_probe = await _resolve_run_time(service, force=force)
            t_probe = time.perf_counter()
            # If we just locked onto a different cycle, drop the frame
            # cache so the animation stays internally consistent.
            cycle_changed = (
                run_time_holder is not None and run_time != run_time_holder
            )
            if cycle_changed:
                set_frames({})
            set_run_time_holder(run_time)

            label = f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
            request = ForecastRequest(run_time=run_time, step_hours=step, param="msl")
            hit_cache = service.is_cached(request)
            set_progress(
                f"Fetching MSL · {run_time:%Y%m%d %Hz} · {_step_label(step)}"
                f"{' (cached)' if hit_cache and not force else ''}…"
            )
            ds = await service.fetch(request, force=force)
            t_fetch = time.perf_counter()
            set_progress("Rendering chart (matplotlib + cartopy)…")
            fig = await asyncio.to_thread(
                render_msl, ds, projection="robinson", run_id=label,
            )
            t_render = time.perf_counter()
            set_progress("Encoding PNG…")
            png_bytes = await asyncio.to_thread(_figure_to_png_bytes, fig)
            t_encode = time.perf_counter()

            set_image_bytes(png_bytes)
            set_run_label(label)
            # Cache this frame; cycle invariance is preserved because we
            # just dropped the dict above if the cycle changed.
            if cycle_changed:
                set_frames({step: png_bytes})
            else:
                set_frames(lambda prev: {**prev, step: png_bytes})
            set_progress("")
            set_state("ready")
            logger.info(
                "Map load timing (step=%dh, probed=%s): probe=%.2fs "
                "fetch+decode=%.2fs render=%.2fs encode=%.2fs total=%.2fs",
                step,
                did_probe,
                t_probe - t0,
                t_fetch - t_probe,
                t_render - t_fetch,
                t_encode - t_render,
                t_encode - t0,
            )
        except Exception as e:
            logger.exception("Map view failed to load forecast (step=%dh)", step)
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    async def load_all_steps_and_play():
        """Pre-fetch every step in STEP_OPTIONS, then start animation.

        Stays in "ready" state throughout so the chart, slider, and label
        stay visible. Each frame's display update happens AFTER its image
        is rendered — the slider does not move ahead of the picture.
        """
        # Don't transition to "loading"; that would hide the image. We
        # stay in "ready" and surface progress via the progress overlay.
        set_error(None)
        set_is_playing(False)
        try:
            service = ForecastService(settings)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            return

        try:
            run_time, _ = await _resolve_run_time(service, force=False)
            # New cycle → drop the dict so half-cached old-run frames
            # don't get mixed in.
            if run_time_holder is not None and run_time != run_time_holder:
                set_frames({})
                new_frames = {}
            else:
                new_frames = dict(frames)
            set_run_time_holder(run_time)

            for i, step in enumerate(STEP_OPTIONS):
                if step in new_frames:
                    # Already loaded — sweep through visually so the user
                    # sees the timeline progress for cached frames too.
                    set_step_hours(step)
                    set_image_bytes(new_frames[step])
                    set_run_label(
                        f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
                    )
                    continue
                req = ForecastRequest(run_time=run_time, step_hours=step, param="msl")
                hit_cache = service.is_cached(req)
                set_progress(
                    f"Loading frame {i + 1}/{len(STEP_OPTIONS)} · "
                    f"{_step_label(step)}{' (cached)' if hit_cache else ''}…"
                )
                png = await _render_step(service, run_time, step)
                # Image ready — only NOW advance the slider/label to this
                # step. The display stays on the previous frame until the
                # new one has been fully rendered and encoded.
                new_frames[step] = png
                set_frames(dict(new_frames))
                set_step_hours(step)
                set_image_bytes(png)
                set_run_label(
                    f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
                )
                # Polite spacing only after frames we actually downloaded —
                # cache hits don't touch S3, no need to slow them down.
                if not hit_cache and i + 1 < len(STEP_OPTIONS):
                    await asyncio.sleep(PRELOAD_SPACING_SEC)

            set_progress("")
            set_is_playing(True)
        except Exception as e:
            logger.exception("Map view failed to preload animation frames")
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    async def _advance_one_frame():
        """One animation tick: sleep, then advance to next step."""
        try:
            await asyncio.sleep(FRAME_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
        try:
            idx = STEP_OPTIONS.index(step_hours)
        except ValueError:
            return
        next_step = STEP_OPTIONS[(idx + 1) % len(STEP_OPTIONS)]
        next_png = frames.get(next_step)
        if next_png is None:
            # Missing frame — stop rather than stall.
            set_is_playing(False)
            return
        set_step_hours(next_step)
        set_image_bytes(next_png)
        set_run_label(
            f"{run_time_holder:%Y%m%d %Hz} IFS · {_step_label(next_step)}"
            if run_time_holder else _step_label(next_step)
        )

    def _animation_setup():
        # The advance task itself mutates step_hours, which re-fires this
        # effect with a fresh closure and schedules the next tick. Pausing
        # flips is_playing → the next setup returns without scheduling and
        # the chain terminates.
        if not is_playing or state != "ready":
            return
        anim_task_ref["task"] = ft.context.page.run_task(_advance_one_frame)

    def _animation_cleanup():
        # Flet runs this before each new setup (when deps change) and at
        # unmount. Cancel any pending tick so paused/seeked animations
        # don't continue advancing in the background.
        task = anim_task_ref.get("task")
        if task is not None and not task.done():
            task.cancel()
        anim_task_ref["task"] = None

    ft.use_effect(
        _animation_setup,
        [is_playing, step_hours, state],
        _animation_cleanup,
    )

    def handle_slider_change(e):
        idx = int(e.control.value)
        new_step = STEP_OPTIONS[idx]
        if new_step == step_hours:
            return
        set_is_playing(False)
        set_step_hours(new_step)
        cached = frames.get(new_step)
        if cached is not None:
            set_image_bytes(cached)
            set_run_label(
                f"{run_time_holder:%Y%m%d %Hz} IFS · {_step_label(new_step)}"
                if run_time_holder else _step_label(new_step)
            )
        else:
            ft.context.page.run_task(load, step=new_step)

    def step_by(delta: int):
        idx = STEP_OPTIONS.index(step_hours)
        new_step = STEP_OPTIONS[(idx + delta) % len(STEP_OPTIONS)]
        set_is_playing(False)
        set_step_hours(new_step)
        cached = frames.get(new_step)
        if cached is not None:
            set_image_bytes(cached)
            set_run_label(
                f"{run_time_holder:%Y%m%d %Hz} IFS · {_step_label(new_step)}"
                if run_time_holder else _step_label(new_step)
            )
        else:
            ft.context.page.run_task(load, step=new_step)

    def toggle_play(_):
        if is_playing:
            set_is_playing(False)
            return
        missing = [s for s in STEP_OPTIONS if s not in frames]
        if missing:
            ft.context.page.run_task(load_all_steps_and_play)
        else:
            set_is_playing(True)

    if state == "disabled":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    error or "Forecast source is not configured.",
                    color=ft.Colors.GREY,
                ),
                ft.Text(
                    "Set forecast_source in config.toml to enable map views.",
                    color=ft.Colors.GREY,
                ),
            ],
        )

    if state == "idle":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "MSL (mean sea level pressure) from the latest ECMWF IFS run. "
                    f"Animation covers T+0h..T+240h at 3h cadence "
                    f"({len(STEP_OPTIONS)} frames).",
                    color=ft.Colors.GREY,
                ),
                ft.FilledButton(
                    content=ft.Text("取得 / Fetch (T+0h)"),
                    on_click=lambda _: ft.context.page.run_task(load, step=0),
                ),
            ],
        )

    if state == "loading":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Row(
                    controls=[
                        ft.ProgressRing(width=20, height=20),
                        ft.Text(progress or "Loading…", color=ft.Colors.GREY),
                    ],
                    spacing=12,
                ),
            ],
        )

    if state == "error":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not render map: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    content=ft.Text("再取得 / Retry"),
                    on_click=lambda _: ft.context.page.run_task(load, step=step_hours, force=True),
                ),
            ],
        )

    # state == "ready"
    try:
        current_idx = STEP_OPTIONS.index(step_hours)
    except ValueError:
        current_idx = 0
    loaded_count = len(frames)
    total_count = len(STEP_OPTIONS)
    all_loaded = loaded_count >= total_count
    is_preloading = bool(progress)  # non-empty progress text = work in flight
    play_icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
    play_tooltip = "一時停止" if is_playing else (
        "▶ アニメーション再生" if all_loaded
        else f"全フレームを読み込んで再生 ({loaded_count}/{total_count})"
    )

    timeline = ft.Row(
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=4,
        controls=[
            ft.IconButton(
                icon=ft.Icons.SKIP_PREVIOUS,
                tooltip="前のフレーム",
                on_click=lambda _: step_by(-1),
            ),
            ft.IconButton(
                icon=play_icon,
                tooltip=play_tooltip,
                on_click=toggle_play,
            ),
            ft.IconButton(
                icon=ft.Icons.SKIP_NEXT,
                tooltip="次のフレーム",
                on_click=lambda _: step_by(1),
            ),
            ft.Container(
                # on_change_end fires once on release rather than on
                # every drag tick — avoids spamming fetches when the
                # user drags through multiple uncached frames.
                content=ft.Slider(
                    min=0,
                    max=total_count - 1,
                    divisions=total_count - 1,
                    value=current_idx,
                    on_change_end=handle_slider_change,
                ),
                expand=True,
            ),
            ft.Text(
                _step_label(step_hours),
                size=12,
                width=160,
                text_align=ft.TextAlign.RIGHT,
            ),
        ],
    )

    return ft.Column(
        expand=True,
        controls=[
            ft.Row(
                controls=[
                    ft.Text(
                        f"MSL · ECMWF IFS · {run_label}",
                        size=16, weight=ft.FontWeight.BOLD,
                    ),
                    ft.FilledButton(
                        content=ft.Text("再取得"),
                        on_click=lambda _: ft.context.page.run_task(
                            load, step=step_hours, force=True,
                        ),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Row(
                visible=is_preloading,
                spacing=8,
                controls=[
                    ft.ProgressRing(width=14, height=14),
                    ft.Text(progress, size=12, color=ft.Colors.GREY),
                ],
            ),
            ft.Container(
                content=ft.Image(
                    src=image_bytes,
                    fit=ft.BoxFit.CONTAIN,
                    expand=True,
                ),
                expand=True,
            ),
            timeline,
        ],
    )
