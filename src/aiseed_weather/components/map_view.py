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
from dataclasses import dataclass

import flet as ft
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from aiseed_weather.figures.msl_chart import render_msl
from aiseed_weather.figures.regions import (
    GLOBAL,
    PRESETS as REGION_PRESETS,
    Region,
    by_key as region_by_key,
    custom_region,
)
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.products.catalog import (
    CATEGORY_LABELS,
    STATUS_LABELS,
    Status,
    Tab as ProductTab,
    by_key as product_by_key,
    grouped_by_category,
)
from aiseed_weather.services.forecast_service import (
    ForecastDisabledError,
    ForecastRequest,
    ForecastService,
)


# Layer / variable choices. Only MSL is wired through to a figure builder
# today; the rest are listed so the UI reflects the planned roadmap (and
# so adding a builder is the only thing standing between us and them
# lighting up).
@dataclass(frozen=True)
class LayerOption:
    key: str
    label: str
    available: bool


LAYER_OPTIONS: tuple[LayerOption, ...] = (
    LayerOption("msl", "MSL — 海面更正気圧 / Mean sea level pressure", True),
    LayerOption("t2m", "T2m — 2m気温 / 2-metre temperature", False),
    LayerOption("gh500", "Z500 — 500hPa高度 / 500 hPa geopotential", False),
    LayerOption("wind10m", "10m風 / 10-metre wind", False),
    LayerOption("tp", "降水 / Total precipitation", False),
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

    # User-facing selectors. region drives the chart projection + extent;
    # layer will eventually pick which figures/ builder to call.
    region, set_region = ft.use_state(GLOBAL)
    layer, set_layer = ft.use_state("msl")
    show_region_dialog, set_show_region_dialog = ft.use_state(False)
    show_time_dialog, set_show_time_dialog = ft.use_state(False)

    # Selected product (data product within this tab). Today only
    # ecmwf_hres is wired through; selecting a planned product just
    # updates the display so the user can browse the catalog.
    product_key, set_product_key = ft.use_state("ecmwf_hres")
    show_catalog_dialog, set_show_catalog_dialog = ft.use_state(False)
    selected_product = product_by_key(product_key)

    async def _render_step(service, run_time, step: int, region_: Region) -> bytes:
        request = ForecastRequest(run_time=run_time, step_hours=step, param="msl")
        ds = await service.fetch(request)
        label = f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
        fig = await asyncio.to_thread(
            render_msl, ds, region=region_, run_id=label,
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

    async def load(*, step: int, region_: Region | None = None, force: bool = False):
        """Fetch + render a single step. Caches the resulting PNG.

        ``region_`` defaults to the current region state at call time, but
        callers that need a different region (e.g. immediately after the
        user picks a new region in the dialog, before re-render captures
        the new state) can pass it explicitly.
        """
        region_used = region_ if region_ is not None else region
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
            set_progress(f"Rendering chart ({region_used.label})…")
            fig = await asyncio.to_thread(
                render_msl, ds, region=region_used, run_id=label,
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
                png = await _render_step(service, run_time, step, region)
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

    def apply_region(new_region: Region):
        # Region change invalidates the rendered-PNG cache (different
        # projection/extent → different image) but NOT the GRIB2 cache
        # on disk. Re-rendering all 65 frames means CPU work, not network
        # traffic, so it's fast-ish (~5 min cold render).
        if new_region == region:
            set_show_region_dialog(False)
            return
        set_region(new_region)
        set_frames({})
        set_show_region_dialog(False)
        # Re-render the currently-displayed step in the new region so
        # the user sees the change immediately.
        if run_time_holder is not None:
            ft.context.page.run_task(
                load, step=step_hours, region_=new_region, force=False,
            )

    def toggle_play(_):
        if is_playing:
            set_is_playing(False)
            return
        missing = [s for s in STEP_OPTIONS if s not in frames]
        if missing:
            ft.context.page.run_task(load_all_steps_and_play)
        else:
            set_is_playing(True)

    # ----- Dialogs (region + time) -----
    # Local draft state so canceling the dialog doesn't mutate the
    # committed selection.
    draft_region_key, set_draft_region_key = ft.use_state(region.key)
    draft_bounds_text, set_draft_bounds_text = ft.use_state(
        # lon_min, lon_max, lat_min, lat_max
        ", ".join(str(int(v)) for v in (region.extent or (-180, 180, -90, 90)))
    )

    def _open_region_dialog():
        # Reset draft to the current committed state each time we open.
        set_draft_region_key(region.key)
        set_draft_bounds_text(
            ", ".join(str(int(v)) for v in (region.extent or (-180, 180, -90, 90)))
        )
        set_show_region_dialog(True)

    def _commit_region(_):
        if draft_region_key == "custom":
            try:
                parts = [float(p.strip()) for p in draft_bounds_text.split(",")]
                if len(parts) != 4:
                    raise ValueError("need 4 comma-separated numbers")
                new_region = custom_region(*parts)
            except (ValueError, TypeError) as e:
                logger.warning("Invalid custom region bounds: %s", e)
                return  # leave dialog open so user can correct
        else:
            new_region = region_by_key(draft_region_key)
        apply_region(new_region)

    def _select_product(key: str):
        set_product_key(key)
        set_show_catalog_dialog(False)
        # Today only ECMWF HRES is wired through. If the user picks a
        # planned product, we update the display and the chart area
        # explains why nothing changed. No silent failure.

    def _build_product_card(p) -> ft.Control:
        # One row in the catalog dialog: status icon + name + spec + meta.
        is_selectable = p.status == Status.IMPLEMENTED
        is_current = p.key == product_key
        # Status-tinted accent so the user can scan implemented vs planned
        # at a glance.
        if p.status == Status.IMPLEMENTED:
            accent = ft.Colors.GREEN
        elif p.status == Status.PLANNED:
            accent = ft.Colors.AMBER
        elif p.status == Status.EXTERNAL_DEP:
            accent = ft.Colors.BLUE_GREY
        else:
            accent = ft.Colors.OUTLINE
        return ft.Container(
            padding=ft.Padding.all(10),
            border=ft.Border.all(
                width=2 if is_current else 1,
                color=ft.Colors.PRIMARY if is_current else ft.Colors.OUTLINE_VARIANT,
            ),
            border_radius=6,
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(
                                width=4, height=18, bgcolor=accent,
                                border_radius=2,
                            ),
                            ft.Text(
                                p.bilingual_label(),
                                size=13, weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                            ft.Text(
                                STATUS_LABELS[p.status],
                                size=10, color=ft.Colors.GREY,
                            ),
                        ],
                        spacing=8,
                    ),
                    ft.Text(
                        p.spec, size=11,
                    ),
                    ft.Text(
                        f"{p.agency} · {p.backend}",
                        size=10, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        f"License: {p.license_info} · {p.source_url}",
                        size=10, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        p.notes, size=10, color=ft.Colors.GREY, italic=True,
                        visible=bool(p.notes),
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.FilledTonalButton(
                                "選択中 / Current" if is_current else "選択 / Select",
                                disabled=(not is_selectable) or is_current,
                                on_click=(
                                    (lambda _, k=p.key: _select_product(k))
                                    if is_selectable else None
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        )

    # Build catalog dialog body — sections by category, cards inside.
    catalog_sections: list[ft.Control] = []
    for cat, products in grouped_by_category(ProductTab.MODELS):
        catalog_sections.append(
            ft.Text(
                CATEGORY_LABELS[cat],
                size=12, weight=ft.FontWeight.BOLD,
                color=ft.Colors.GREY,
            ),
        )
        for p in products:
            catalog_sections.append(_build_product_card(p))
        catalog_sections.append(ft.Container(height=4))

    catalog_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("プロダクト選択 / Select product"),
        content=ft.Container(
            width=620,
            height=520,
            content=ft.Column(
                scroll=ft.ScrollMode.ADAPTIVE,
                spacing=8,
                controls=catalog_sections,
            ),
        ),
        actions=[
            ft.TextButton(
                "閉じる / Close",
                on_click=lambda _: set_show_catalog_dialog(False),
            ),
        ],
    )

    region_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("地域設定 / Region Settings"),
        content=ft.Column(
            tight=True,
            width=420,
            controls=[
                ft.Text(
                    "プリセットから選択するか、カスタム範囲を指定してください。",
                    size=12, color=ft.Colors.GREY,
                ),
                ft.RadioGroup(
                    value=draft_region_key,
                    on_change=lambda e: set_draft_region_key(e.control.value),
                    content=ft.Column(
                        tight=True,
                        spacing=2,
                        controls=[
                            ft.Radio(value=r.key, label=r.label)
                            for r in REGION_PRESETS
                        ] + [
                            ft.Radio(value="custom", label="任意 / Custom bounds"),
                        ],
                    ),
                ),
                ft.Container(
                    visible=(draft_region_key == "custom"),
                    padding=ft.Padding.only(left=32, top=4),
                    content=ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text(
                                "lon_min, lon_max, lat_min, lat_max  "
                                "(degrees, PlateCarree)",
                                size=10, color=ft.Colors.GREY,
                            ),
                            ft.TextField(
                                value=draft_bounds_text,
                                hint_text="115, 155, 20, 50",
                                dense=True,
                                on_change=lambda e: set_draft_bounds_text(
                                    e.control.value,
                                ),
                            ),
                        ],
                    ),
                ),
            ],
        ),
        actions=[
            ft.TextButton(
                "キャンセル",
                on_click=lambda _: set_show_region_dialog(False),
            ),
            ft.FilledButton("適用 / Apply", on_click=_commit_region),
        ],
    )

    time_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("時間設定 / Time Settings"),
        content=ft.Column(
            tight=True,
            width=420,
            controls=[
                ft.Text("Run / cycle", size=11, color=ft.Colors.GREY),
                ft.Text(
                    f"{run_time_holder:%Y-%m-%d %H:%M UTC} IFS"
                    if run_time_holder else "Not loaded yet",
                    size=14, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "Auto-pick: latest fully-published cycle (need T+240h)",
                    size=11, color=ft.Colors.GREY,
                ),
                ft.Divider(height=12),
                ft.Text("Cycle override", size=11, color=ft.Colors.GREY),
                ft.RadioGroup(
                    value="auto",
                    content=ft.Column(
                        tight=True,
                        spacing=2,
                        controls=[
                            ft.Radio(
                                value="auto",
                                label="Auto (recommended)",
                            ),
                            ft.Radio(
                                value="00z", label="00z (disabled · future)",
                                disabled=True,
                            ),
                            ft.Radio(
                                value="06z", label="06z (disabled · future)",
                                disabled=True,
                            ),
                            ft.Radio(
                                value="12z", label="12z (disabled · future)",
                                disabled=True,
                            ),
                            ft.Radio(
                                value="18z", label="18z (disabled · future)",
                                disabled=True,
                            ),
                        ],
                    ),
                ),
                ft.Text(
                    "Manual cycle override lands in a follow-up: today the "
                    "app always picks the newest cycle that has the full "
                    "forecast horizon published.",
                    size=10, color=ft.Colors.GREY, italic=True,
                ),
                ft.Divider(height=12),
                ft.Text("Animation range", size=11, color=ft.Colors.GREY),
                ft.Text(
                    f"T+0h .. T+{MAX_STEP}h, 3h cadence  "
                    f"({len(STEP_OPTIONS)} frames)",
                    size=12,
                ),
                ft.Text(
                    "Range and cadence selectors land in a follow-up.",
                    size=10, color=ft.Colors.GREY, italic=True,
                ),
            ],
        ),
        actions=[
            ft.TextButton(
                "閉じる",
                on_click=lambda _: set_show_time_dialog(False),
            ),
        ],
    )

    # Show whichever dialog is open. Passing None dismisses; the hook
    # diffs the dialog dataclass field-by-field, so re-rendering with
    # updated draft state preserves cursor / focus.
    ft.use_dialog(
        catalog_dialog if show_catalog_dialog
        else region_dialog if show_region_dialog
        else time_dialog if show_time_dialog
        else None
    )

    # ----- Layout -----
    # Desktop 3-pane shell:
    #   left  : control panel  (fixed 240px, layer / region / time / action)
    #   main  : chart + timeline (expand)
    #   bottom: status bar (90px, progress + cache + cycle info)
    # The disabled state bypasses the shell and shows a full-pane hint.

    if state == "disabled":
        return ft.SafeArea(
            expand=True,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
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
            ),
        )

    try:
        current_idx = STEP_OPTIONS.index(step_hours)
    except ValueError:
        current_idx = 0
    loaded_count = len(frames)
    total_count = len(STEP_OPTIONS)
    all_loaded = loaded_count >= total_count
    has_image = image_bytes is not None
    is_working = bool(progress)
    play_icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
    play_tooltip = "一時停止" if is_playing else (
        "▶ アニメーション再生" if all_loaded
        else f"全フレームを読み込んで再生 ({loaded_count}/{total_count})"
    )

    # ----- Left: control panel -----
    if state == "error":
        primary_action = ft.FilledButton(
            content=ft.Text("再取得 / Retry"),
            width=208,
            on_click=lambda _: ft.context.page.run_task(
                load, step=step_hours, force=True,
            ),
        )
    elif not has_image:
        primary_action = ft.FilledButton(
            content=ft.Text("取得 / Fetch (T+0h)"),
            width=208,
            disabled=(state == "loading"),
            on_click=lambda _: ft.context.page.run_task(load, step=0),
        )
    else:
        primary_action = ft.FilledButton(
            content=ft.Text("再取得 / Refresh"),
            width=208,
            disabled=is_working,
            on_click=lambda _: ft.context.page.run_task(
                load, step=step_hours, force=True,
            ),
        )

    # Layer dropdown — only the MSL option is functional today; the rest
    # advertise the planned variables and are disabled.
    layer_dropdown = ft.Dropdown(
        label="レイヤー / Layer",
        value=layer,
        dense=True,
        options=[
            ft.dropdown.Option(
                key=opt.key,
                text=opt.label + ("" if opt.available else "  (近日)"),
                disabled=not opt.available,
            )
            for opt in LAYER_OPTIONS
        ],
        on_select=lambda e: set_layer(e.control.value),
    )

    region_dropdown = ft.Dropdown(
        label="地域 / Region",
        value=(region.key if region.key != "custom" else None),
        hint_text=region.label if region.key == "custom" else None,
        dense=True,
        options=[
            ft.dropdown.Option(key=r.key, text=r.label)
            for r in REGION_PRESETS
        ],
        on_select=lambda e: apply_region(region_by_key(e.control.value)),
    )

    # Status badge for the currently-selected product (green for fully
    # wired through, amber for "planned, viewing only").
    if selected_product.status == Status.IMPLEMENTED:
        product_status_color = ft.Colors.GREEN
        product_status_text = "実装済み / wired"
    else:
        product_status_color = ft.Colors.AMBER
        product_status_text = "閲覧のみ / catalog only"

    control_panel = ft.Container(
        width=240,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        padding=ft.Padding.all(16),
        content=ft.Column(
            spacing=8,
            scroll=ft.ScrollMode.ADAPTIVE,
            controls=[
                ft.Text(
                    "モデル / Models", size=16, weight=ft.FontWeight.BOLD,
                ),

                ft.Divider(height=14),
                ft.Text(
                    "プロダクト / Product", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    selected_product.bilingual_label(),
                    size=12, weight=ft.FontWeight.BOLD,
                ),
                ft.Row(
                    spacing=6,
                    controls=[
                        ft.Container(
                            width=8, height=8, bgcolor=product_status_color,
                            border_radius=4,
                        ),
                        ft.Text(
                            product_status_text,
                            size=10, color=ft.Colors.GREY,
                        ),
                    ],
                ),
                ft.Text(
                    selected_product.spec,
                    size=10, color=ft.Colors.GREY,
                ),
                ft.TextButton(
                    content=ft.Text("モデル変更 / Change…", size=12),
                    icon=ft.Icons.LIST_ALT,
                    on_click=lambda _: set_show_catalog_dialog(True),
                ),

                ft.Divider(height=14),
                ft.Text(
                    "レイヤー / Layer", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                layer_dropdown,

                ft.Divider(height=14),
                ft.Text(
                    "地域 / Region", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                region_dropdown,
                ft.TextButton(
                    content=ft.Text("詳細設定 / Custom bounds…", size=12),
                    icon=ft.Icons.TUNE,
                    on_click=lambda _: _open_region_dialog(),
                ),

                ft.Divider(height=14),
                ft.Text(
                    "時間 / Time", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    f"{run_time_holder:%Y%m%d %Hz} IFS"
                    if run_time_holder else "Not loaded",
                    size=12, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    f"Animation: T+0..T+{MAX_STEP}h at 3h ({total_count} frames)",
                    size=10, color=ft.Colors.GREY,
                ),
                ft.TextButton(
                    content=ft.Text("詳細設定 / Time settings…", size=12),
                    icon=ft.Icons.SCHEDULE,
                    on_click=lambda _: set_show_time_dialog(True),
                ),

                ft.Divider(height=14),
                primary_action,
            ],
        ),
    )

    # ----- Main: chart area depending on state -----
    if state == "idle":
        main_area = ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Icon(
                        ft.Icons.PUBLIC, size=64, color=ft.Colors.OUTLINE_VARIANT,
                    ),
                    ft.Text(
                        "Press 取得 / Fetch to load the latest analysis.",
                        size=14, color=ft.Colors.GREY,
                    ),
                ],
            ),
        )
    elif state == "loading" and not has_image:
        # Very first fetch — no chart to show yet.
        main_area = ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.ProgressRing(width=20, height=20),
                    ft.Text(progress or "Loading…", color=ft.Colors.GREY),
                ],
            ),
        )
    elif state == "error":
        main_area = ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Icon(
                        ft.Icons.ERROR_OUTLINE, size=48, color=ft.Colors.RED,
                    ),
                    ft.Text(
                        f"Could not render map: {error}",
                        color=ft.Colors.RED, size=13,
                    ),
                ],
            ),
        )
    else:
        # state == "ready" (or "loading" with an existing image we keep)
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
        main_area = ft.Column(
            expand=True,
            spacing=4,
            controls=[
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                    content=ft.Text(
                        f"MSL · ECMWF IFS · {run_label}",
                        size=15, weight=ft.FontWeight.BOLD,
                    ),
                ),
                ft.Container(
                    content=ft.Image(
                        src=image_bytes,
                        fit=ft.BoxFit.CONTAIN,
                        expand=True,
                    ),
                    expand=True,
                ),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=8, vertical=0),
                    content=timeline,
                ),
            ],
        )

    # ----- Bottom: status bar -----
    if state == "error":
        status_primary = ft.Text(error, size=12, color=ft.Colors.RED)
    elif is_working:
        status_primary = ft.Text(progress, size=12)
    elif state == "ready" and is_playing:
        status_primary = ft.Text(
            f"Playing · {_step_label(step_hours)}", size=12,
        )
    elif state == "ready":
        status_primary = ft.Text(
            f"Ready · viewing {_step_label(step_hours)}", size=12,
        )
    else:
        status_primary = ft.Text("Idle", size=12, color=ft.Colors.GREY)

    status_bar = ft.Container(
        height=90,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        content=ft.Column(
            spacing=4,
            controls=[
                ft.Row(
                    spacing=10,
                    controls=[
                        ft.ProgressRing(
                            width=14, height=14, visible=is_working,
                        ),
                        status_primary,
                    ],
                ),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text(
                            f"Frames cached: {loaded_count}/{total_count}",
                            size=11, color=ft.Colors.GREY,
                        ),
                        ft.Text(
                            (
                                f"Cycle: {run_time_holder:%Y%m%d %Hz} IFS"
                                if run_time_holder else "Cycle: —"
                            ),
                            size=11, color=ft.Colors.GREY,
                        ),
                        ft.Text(
                            f"Source: {settings.forecast_source.value}",
                            size=11, color=ft.Colors.GREY,
                        ),
                    ],
                ),
            ],
        ),
    )

    return ft.Row(
        expand=True,
        spacing=0,
        controls=[
            control_panel,
            ft.VerticalDivider(width=1),
            ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    ft.Container(content=main_area, expand=True),
                    ft.Divider(height=1),
                    status_bar,
                ],
            ),
        ],
    )
