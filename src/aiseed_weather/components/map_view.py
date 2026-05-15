# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Map view: ECMWF synoptic chart with timeline animation.

Pipeline (only after the user presses 取得 / Fetch):
  probe latest run via ForecastService.latest_run → download GRIB2 →
  decode via cfgrib → render matplotlib figure → embed as PNG bytes.

Animation:
- After the first single-step fetch, the user can press
  ▶ アニメーション to pre-load every step the user chose in the Data
  dialog (horizon × cadence → step_options).
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
    FIELDS as DATA_FIELDS,
    STATUS_LABELS,
    Status,
    Tab as ProductTab,
    by_key as product_by_key,
    field_by_key,
    grouped_by_category,
)
from aiseed_weather.services.forecast_service import (
    _CLIENT_SOURCE,
    ForecastDisabledError,
    ForecastRequest,
    ForecastService,
    is_grib_cached,
    probe_cycle_complete,
)


logger = logging.getLogger(__name__)


# HRES 0p25 oper publishes step=0..144 every 3h and step=150..240 every 6h.
# We let the user trade animation smoothness for preload time by picking
# both a max horizon and a cadence; the full 3h × T+240h corresponds to
# 65 frames and roughly 10 min cold preload on a typical laptop.
FRAME_INTERVAL_SEC = 0.9  # animation playback frame duration
# Spacing between consecutive S3 fetches during preload. S3's per-prefix
# rate limit triggers "503 Slow Down" if we hammer one bucket prefix; a
# small pause between requests keeps the rate below that threshold and
# spares us from multi-second retry backoffs.
PRELOAD_SPACING_SEC = 0.5

# Choices we expose in the Data dialog. Horizon caps where the animation
# stops; cadence is the spacing between frames. ECMWF only publishes 6h
# after T+144h, so a 3h cadence past 144h actually yields 6h there.
HORIZON_CHOICES_H: tuple[int, ...] = (24, 48, 72, 120, 168, 240, 360)
CADENCE_CHOICES_H: tuple[int, ...] = (3, 6, 12, 24)


def _compute_steps(max_h: int, cadence_h: int) -> tuple[int, ...]:
    """Build the (sorted, unique) step list for the slider/animation.

    Honours ECMWF Open Data's publication cadence: every 3h up to T+144h,
    every 6h thereafter. A user-requested 3h cadence past 144h is clamped
    to 6h since smaller is not published. A user-requested 24h cadence
    just snaps to the nearest available step.
    """
    steps: list[int] = []
    # Pre-144 stretch uses user's cadence (but at least 1h).
    h = 0
    upper_short = min(144, max_h)
    while h <= upper_short:
        steps.append(h)
        h += max(cadence_h, 1)
    # Post-144 stretch: 6h minimum from upstream.
    if max_h > 144:
        post_cadence = max(cadence_h, 6)
        h = 150 if 150 <= max_h else max_h + 1
        while h <= max_h:
            steps.append(h)
            h += post_cadence
    # Always include max_h as the final frame if it's not already there.
    if steps and steps[-1] != max_h and max_h <= 240:
        steps.append(max_h)
    return tuple(sorted(set(steps)))


def _recent_base_times(n: int = 20):
    """Compute the last `n` synoptic cycles (00/06/12/18 UTC) ending now.

    Doesn't probe the server — these are theoretical cycle stamps. Some
    may not yet be published; the user finds out at fetch time. Listing
    them all is more useful than guessing publication status here.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    cycle_hour = (now.hour // 6) * 6
    latest = now.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    return [latest - timedelta(hours=6 * i) for i in range(n)]


# HRES IFS 0p25 oper publishes 4 cycles per day with two different
# forecast horizons. 00z and 12z run to T+360h (15 days); 06z and 18z
# stop at T+144h (6 days), giving forecasters short-range refreshes
# between the long cycles. To get a smooth animation past T+144h from
# a short cycle, we stitch its short-range steps with extension steps
# from the most recent long cycle preceding it.
#
# Publication is ATOMIC per cycle — every step's GRIB2 appears with
# the same scheduled timestamp on the dissemination index. Lag from
# nominal cycle time to that timestamp depends on cycle hour:
#   00z, 12z (T+360h, ~85 files)  →  ~7.5h
#   06z, 18z (T+144h, ~49 files)  →  ~6.5h
# The shorter run finishes faster.
#
# Empirical reference (from data.ecmwf.int dissemination index):
#   20260514 00z run scheduled 14-05-2026 07:34 (= cycle + 7h34m)
#   20260514 06z run scheduled 14-05-2026 12:27 (= cycle + 6h27m)
_LONG_CYCLE_HORIZON_H = 360
_SHORT_CYCLE_HORIZON_H = 144
_LONG_CYCLE_HOURS = (0, 12)
_SHORT_CYCLE_HOURS = (6, 18)
_PUBLICATION_LAG_LONG_H = 7.5   # cycle → atomic publication, 00z / 12z
_PUBLICATION_LAG_SHORT_H = 6.5  # cycle → atomic publication, 06z / 18z


def _is_short_cycle(cycle_dt) -> bool:
    return cycle_dt.hour in _SHORT_CYCLE_HOURS


def _cycle_horizon_h(cycle_dt) -> int:
    return (
        _SHORT_CYCLE_HORIZON_H if cycle_dt.hour in _SHORT_CYCLE_HOURS
        else _LONG_CYCLE_HORIZON_H
    )


def _prior_long_cycle(cycle_dt):
    """The most recent 00z/12z cycle strictly before ``cycle_dt``.

    For 06z → previous 00z (6h earlier). For 18z → previous 12z (6h
    earlier). Both other inputs are already long cycles, in which case
    this returns 12 hours earlier (the previous long cycle).
    """
    from datetime import timedelta
    dt = cycle_dt - timedelta(hours=6)
    while dt.hour not in _LONG_CYCLE_HOURS:
        dt -= timedelta(hours=6)
    return dt


def _publication_time(cycle_dt):
    """Approximate single UTC time when this cycle becomes available.

    ECMWF Open Data publishes a cycle ATOMICALLY — all GRIB2 files for
    a cycle appear on the dissemination index with the same scheduled
    timestamp. Empirical reference times observed on data.ecmwf.int:

      20260514 00z run scheduled 14-05-2026 07:34 → +7h34m
      20260514 06z run scheduled 14-05-2026 12:27 → +6h27m

    Long cycles (00z/12z) take longer because they produce ~85 GRIB2
    files reaching T+360h; short cycles (06z/18z) only produce ~49
    files reaching T+144h.
    """
    from datetime import timedelta
    lag = (
        _PUBLICATION_LAG_SHORT_H if cycle_dt.hour in _SHORT_CYCLE_HOURS
        else _PUBLICATION_LAG_LONG_H
    )
    return cycle_dt + timedelta(hours=lag)


def _stitch_plan(
    primary_cycle,
    max_step_h: int,
    cadence_h: int,
) -> tuple[tuple[int, "datetime", int], ...]:
    """Build [(display_step, source_cycle, source_step), ...] for the
    requested horizon, transparently stitching a short primary cycle
    with the previous long cycle for steps beyond the short horizon.

    Display step is the value the slider reports (the "T+Nh" the user
    sees, expressed relative to primary_cycle). source_step is what we
    actually ask the upstream cycle for; it equals display_step inside
    the primary cycle's horizon, and display_step + offset when we've
    crossed into the extension cycle (offset = primary − extension in
    hours, i.e. 6 for the typical 06z→00z or 18z→12z stitch).

    If max_step_h exceeds what stitching can deliver (extension cycle
    also runs out), the plan is truncated.
    """
    primary_horizon = _cycle_horizon_h(primary_cycle)
    if not _is_short_cycle(primary_cycle) or max_step_h <= primary_horizon:
        # No stitching needed.
        return tuple(
            (s, primary_cycle, s)
            for s in _compute_steps(min(max_step_h, primary_horizon), cadence_h)
        )

    extension = _prior_long_cycle(primary_cycle)
    extension_horizon = _cycle_horizon_h(extension)  # 240
    offset_h = int((primary_cycle - extension).total_seconds() / 3600)
    effective_max = min(max_step_h, extension_horizon - offset_h)
    plan = []
    for s in _compute_steps(effective_max, cadence_h):
        if s <= primary_horizon:
            plan.append((s, primary_cycle, s))
        else:
            plan.append((s, extension, s + offset_h))
    return tuple(plan)


def _step_label(h: int) -> str:
    if h == 0:
        return "T+0h (analysis)"
    days = h // 24
    rest = h % 24
    return f"T+{h}h (D+{days})" if rest == 0 else f"T+{h}h"


def _valid_time_display(base_time, step_hours: int) -> str:
    """Format valid time (= base time + lead). Returns '未取得' if no base."""
    if base_time is None:
        return "未取得"
    from datetime import timedelta
    return (base_time + timedelta(hours=step_hours)).strftime("%Y-%m-%d %H:%M UTC")


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

    # User-facing selectors. region drives the chart projection +
    # extent; data_field_key picks which meteorological field to fetch
    # and render (msl, t2m, gh@500, ...).
    region, set_region = ft.use_state(GLOBAL)
    data_field_key, set_data_field_key = ft.use_state("msl")
    show_region_dialog, set_show_region_dialog = ft.use_state(False)
    show_time_dialog, set_show_time_dialog = ft.use_state(False)
    show_data_dialog, set_show_data_dialog = ft.use_state(False)
    selected_field = field_by_key(data_field_key)

    # Selected product (data product within this tab). Today only
    # ecmwf_hres is wired through; selecting a planned product just
    # updates the display so the user can browse the catalog.
    product_key, set_product_key = ft.use_state("ecmwf_hres")
    show_catalog_dialog, set_show_catalog_dialog = ft.use_state(False)
    selected_product = product_by_key(product_key)

    # Selected data source per product. Initial value for ECMWF HRES
    # comes from config.toml (settings.forecast_source) so the user's
    # config-time preference is honoured before they touch the dialog.
    def _initial_source_for(p_key: str) -> str:
        if p_key == "ecmwf_hres":
            # Map ForecastSource enum → ecmwf-opendata Client source string.
            try:
                return _CLIENT_SOURCE[settings.forecast_source]
            except KeyError:
                pass  # NONE etc. — fall through to catalog default
        return product_by_key(p_key).default_source_key

    # Currently fixed at config.toml's choice. We keep state + the
    # override plumbing so a future "advanced" toggle can flip mirrors
    # at runtime, but the mirror is not a user-facing concern: the
    # bytes are identical across AWS/Azure/GCP/ECMWF Direct, only
    # latency differs. What the user cares about is the cycle (run
    # initialization time), so that's what gets the prominent UI.
    data_source_key, set_data_source_key = ft.use_state(
        _initial_source_for("ecmwf_hres")
    )

    # Manual cycle override. When None, _resolve_run_time falls back
    # to auto-probing the latest fully-published cycle (today's behaviour).
    # When set, we use this exact datetime as the run, skipping the probe.
    manual_cycle, set_manual_cycle = ft.use_state(None)
    # Verified availability of recent cycles. {cycle.isoformat(): bool}.
    # Populated by probes kicked off when the data dialog opens; the UI
    # shows real ✓/✗ labels for cycles we've checked, predicted text
    # for cycles we haven't.
    cycle_check_results, set_cycle_check_results = ft.use_state({})

    # Forecast horizon and cadence (drive both the slider and the
    # animation preload size). Defaults match the previous hard-coded
    # 65-frame setup; user can shorten to make preload less painful.
    max_step_h, set_max_step_h = ft.use_state(240)
    cadence_h, set_cadence_h = ft.use_state(3)

    # Stitch plan: for each display step, which actual (cycle, step)
    # to fetch from. Steps within the primary cycle's horizon come from
    # the primary; steps beyond (only happens for short 06z/18z cycles
    # whose user-requested horizon exceeds 90h) come from the previous
    # long cycle. step_options is the list of display steps.
    primary_cycle = manual_cycle if manual_cycle is not None else run_time_holder
    if primary_cycle is not None:
        stitch_plan = _stitch_plan(primary_cycle, max_step_h, cadence_h)
        step_options = tuple(p[0] for p in stitch_plan)
        source_lookup = {p[0]: (p[1], p[2]) for p in stitch_plan}
    else:
        stitch_plan = ()
        step_options = _compute_steps(max_step_h, cadence_h)
        source_lookup = {}
    MAX_STEP = max(step_options) if step_options else 0

    def _source_for(display_step):
        """Return (source_cycle, source_step) for the given display step.
        Falls back to (primary, display) if not in the plan."""
        return source_lookup.get(
            display_step, (primary_cycle, display_step),
        )

    async def _render_one(
        service,
        src_cycle,
        src_step: int,
        region_: Region,
        *,
        display_step: int,
        primary,
    ) -> bytes:
        """Fetch + render one frame from (src_cycle, src_step).

        Decoupled from any source_lookup closure so callers (preload
        loop, single-step fetch) can pass the already-resolved source
        cycle / step directly. The run_id footer credits both the
        primary cycle and the extension cycle when they differ.
        """
        request = ForecastRequest(
            run_time=src_cycle, step_hours=src_step, param="msl",
        )
        ds = await service.fetch(request)
        if src_cycle == primary:
            label = f"{primary:%Y%m%d %Hz} IFS · {_step_label(display_step)}"
        else:
            label = (
                f"{primary:%Y%m%d %Hz} IFS "
                f"+ {src_cycle:%Y%m%d %Hz} ext · {_step_label(display_step)}"
            )
        fig = await asyncio.to_thread(
            render_msl, ds, region=region_, run_id=label,
        )
        return await asyncio.to_thread(_figure_to_png_bytes, fig)

    async def _resolve_run_time(service, *, force: bool):
        """Pick the IFS cycle every frame must come from.

        Three modes, in priority order:
        1. ``manual_cycle`` is set → the user pinned a specific cycle in
           the Time dialog. Use it directly, no probe.
        2. ``run_time_holder`` is set and not forced → reuse the
           previously-probed cycle (Auto mode).
        3. Otherwise → probe latest_run with the LARGEST step so we
           lock onto a cycle that has published its full forecast
           horizon. Smaller-step probes give a fresher cycle but risk
           404s deep into the preload because long-range fields lag.
        """
        if manual_cycle is not None:
            return manual_cycle, False
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
            service = ForecastService(settings, override_source=data_source_key)
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

            # Resolve the actual (source_cycle, source_step) for the
            # requested display step. For long cycles (00z/12z) this is
            # just (run_time, step); for short cycles past their horizon
            # this hops to the extension cycle.
            if step in source_lookup:
                src_cycle, src_step = source_lookup[step]
            elif _is_short_cycle(run_time) and step > _cycle_horizon_h(run_time):
                # Plan not yet built (e.g. before run_time_holder was
                # set). Compute on the fly.
                ext = _prior_long_cycle(run_time)
                offset = int((run_time - ext).total_seconds() / 3600)
                src_cycle, src_step = ext, step + offset
            else:
                src_cycle, src_step = run_time, step
            if src_cycle == run_time:
                label = f"{run_time:%Y%m%d %Hz} IFS · {_step_label(step)}"
            else:
                label = (
                    f"{run_time:%Y%m%d %Hz} IFS "
                    f"+ {src_cycle:%Y%m%d %Hz} ext · {_step_label(step)}"
                )
            request = ForecastRequest(
                run_time=src_cycle, step_hours=src_step, param="msl",
            )
            hit_cache = service.is_cached(request)
            set_progress(
                f"Fetching MSL · {src_cycle:%Y%m%d %Hz} step={src_step}h · "
                f"{_step_label(step)}"
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
        """Pre-fetch every step in step_options, then start animation.

        Stays in "ready" state throughout so the chart, slider, and label
        stay visible. Each frame's display update happens AFTER its image
        is rendered — the slider does not move ahead of the picture.
        """
        # Don't transition to "loading"; that would hide the image. We
        # stay in "ready" and surface progress via the progress overlay.
        set_error(None)
        set_is_playing(False)
        try:
            service = ForecastService(settings, override_source=data_source_key)
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

            # Build the plan against the (possibly newly probed) run_time.
            # source_lookup from the render scope might be stale if
            # primary_cycle was None then.
            local_plan = _stitch_plan(run_time, max_step_h, cadence_h)
            total = len(local_plan)
            local_steps = tuple(p[0] for p in local_plan)

            for i, (display_step, src_cycle, src_step) in enumerate(local_plan):
                if display_step in new_frames:
                    # Already loaded — sweep through visually so the user
                    # sees the timeline progress for cached frames too.
                    set_step_hours(display_step)
                    set_image_bytes(new_frames[display_step])
                    set_run_label(
                        f"{run_time:%Y%m%d %Hz} IFS · {_step_label(display_step)}"
                        + (
                            f" + {src_cycle:%Hz} ext"
                            if src_cycle != run_time else ""
                        )
                    )
                    continue
                req = ForecastRequest(
                    run_time=src_cycle, step_hours=src_step, param="msl",
                )
                hit_cache = service.is_cached(req)
                ext_tag = (
                    f" [ext {src_cycle:%Hz}]" if src_cycle != run_time else ""
                )
                set_progress(
                    f"Loading frame {i + 1}/{total} · "
                    f"{_step_label(display_step)}{ext_tag}"
                    f"{' (cached)' if hit_cache else ''}…"
                )
                # _render_step uses _source_for via the render-scope
                # source_lookup, which is correct for the COMMITTED
                # primary_cycle. Inside this loop, run_time may differ
                # if we just probed a new cycle. Call _render_step with
                # the source we resolved locally instead.
                png = await _render_one(
                    service, src_cycle, src_step, region,
                    display_step=display_step, primary=run_time,
                )
                new_frames[display_step] = png
                set_frames(dict(new_frames))
                set_step_hours(display_step)
                set_image_bytes(png)
                set_run_label(
                    f"{run_time:%Y%m%d %Hz} IFS · {_step_label(display_step)}"
                    + (
                        f" + {src_cycle:%Y%m%d %Hz} ext"
                        if src_cycle != run_time else ""
                    )
                )
                if not hit_cache and i + 1 < total:
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
            idx = step_options.index(step_hours)
        except ValueError:
            return
        next_step = step_options[(idx + 1) % len(step_options)]
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
        new_step = step_options[idx]
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
        idx = step_options.index(step_hours)
        new_step = step_options[(idx + delta) % len(step_options)]
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
        missing = [s for s in step_options if s not in frames]
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
        # Picking a product also picks its default source unless the
        # user has already locked in a non-default for that product.
        # For the active product we keep the current source choice;
        # otherwise we reset to the product's default (or, for HRES,
        # the config.toml-derived initial value).
        set_product_key(key)
        if key != product_key:
            set_data_source_key(_initial_source_for(key))
            # Different product → its frames are not comparable. Drop.
            set_frames({})
        set_show_catalog_dialog(False)
        # Today only ECMWF HRES is wired through. If the user picks a
        # planned product, we update the display and the chart area
        # explains why nothing changed. No silent failure.


    def _build_product_card(p) -> ft.Control:
        # One row in the catalog dialog: status icon + name + spec + meta
        # + per-product data-source dropdown.
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

        # Sources are picked in a separate dialog. Here we just list
        # the available source labels so the user can scan what paths
        # this product offers without leaving the model picker.
        if p.sources:
            sources_summary = ft.Text(
                "取得元 / Sources: " + ", ".join(s.key for s in p.sources)
                + f"  (default: {p.default_source_key})",
                size=10, color=ft.Colors.GREY,
            )
        else:
            sources_summary = ft.Text(
                "取得元未登録 / no sources registered",
                size=10, color=ft.Colors.GREY, italic=True,
            )

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
                    ft.Text(p.spec, size=11),
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
                    sources_summary,
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

    # ----- Data dialog (fields the selected model can provide) -----
    def _select_data_field(key: str):
        if key != data_field_key:
            set_data_field_key(key)
            # Different field → cached PNGs are wrong (we'd be showing
            # the old field's rendering). Drop the cache.
            set_frames({})
        set_show_data_dialog(False)

    def _build_field_card(f) -> ft.Control:
        is_selectable = f.status == Status.IMPLEMENTED
        is_current = f.key == data_field_key
        if f.status == Status.IMPLEMENTED:
            accent = ft.Colors.GREEN
        elif f.status == Status.PLANNED:
            accent = ft.Colors.AMBER
        elif f.status == Status.EXTERNAL_DEP:
            accent = ft.Colors.BLUE_GREY
        else:
            accent = ft.Colors.OUTLINE
        return ft.Container(
            padding=ft.Padding.all(10),
            border=ft.Border.all(
                width=2 if is_current else 1,
                color=(
                    ft.Colors.PRIMARY if is_current
                    else ft.Colors.OUTLINE_VARIANT
                ),
            ),
            border_radius=6,
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.Container(
                                width=4, height=18, bgcolor=accent,
                                border_radius=2,
                            ),
                            ft.Text(
                                f.bilingual_label() + f.level_suffix(),
                                size=13, weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                            ft.Text(
                                STATUS_LABELS[f.status],
                                size=10, color=ft.Colors.GREY,
                            ),
                        ],
                    ),
                    ft.Text(
                        f"短縮名: {f.key} · 単位: {f.unit}",
                        size=11,
                    ),
                    ft.Text(
                        f"既定レイヤー: {f.typical_layer}",
                        size=10, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        f.notes, size=10, color=ft.Colors.GREY, italic=True,
                        visible=bool(f.notes),
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.FilledTonalButton(
                                "選択中 / Current" if is_current else "選択 / Select",
                                disabled=(not is_selectable) or is_current,
                                on_click=(
                                    (lambda _, k=f.key: _select_data_field(k))
                                    if is_selectable else None
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        )

    # Group by surface vs pressure-level for readability.
    surface_fields = [f for f in DATA_FIELDS if f.level is None and not f.key.startswith("thickness") and not f.key.startswith("theta")]
    pressure_fields = [f for f in DATA_FIELDS if f.level is not None]
    derived_fields = [f for f in DATA_FIELDS if f.key.startswith("thickness") or f.key.startswith("theta")]

    data_sections: list[ft.Control] = []
    if surface_fields:
        data_sections.append(
            ft.Text(
                "地表面 / Surface", size=12, weight=ft.FontWeight.BOLD,
                color=ft.Colors.GREY,
            )
        )
        data_sections.extend(_build_field_card(f) for f in surface_fields)
        data_sections.append(ft.Container(height=4))
    if pressure_fields:
        data_sections.append(
            ft.Text(
                "気圧面 / Pressure levels", size=12, weight=ft.FontWeight.BOLD,
                color=ft.Colors.GREY,
            )
        )
        data_sections.extend(_build_field_card(f) for f in pressure_fields)
        data_sections.append(ft.Container(height=4))
    if derived_fields:
        data_sections.append(
            ft.Text(
                "導出量 / Derived", size=12, weight=ft.FontWeight.BOLD,
                color=ft.Colors.GREY,
            )
        )
        data_sections.extend(_build_field_card(f) for f in derived_fields)

    layer_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("レイヤー / Layer (field + style)"),
        content=ft.Container(
            width=600,
            height=520,
            content=ft.Column(
                tight=True,
                scroll=ft.ScrollMode.ADAPTIVE,
                spacing=8,
                controls=[
                    ft.Text(
                        f"プロダクト: {selected_product.bilingual_label()}",
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        "場 (field) と描画スタイルの組み合わせを「レイヤー」"
                        "として選びます。利用可否は実装状況による。",
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Divider(height=8),
                    *data_sections,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                "閉じる / Close",
                on_click=lambda _: set_show_data_dialog(False),
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

    # Draft state for the time dialog. Initialised from the committed
    # state every time the dialog opens, so Cancel never mutates the
    # live values.
    from datetime import datetime, timezone, timedelta

    _BASE_TIME_CHOICES = _recent_base_times(20)
    _current_base_iso = (
        manual_cycle.isoformat() if manual_cycle is not None
        else (run_time_holder.isoformat() if run_time_holder is not None else "auto")
    )

    draft_base_choice, set_draft_base_choice = ft.use_state(
        "auto" if manual_cycle is None else manual_cycle.isoformat()
    )
    draft_horizon_h, set_draft_horizon_h = ft.use_state(max_step_h)
    draft_cadence_h, set_draft_cadence_h = ft.use_state(cadence_h)

    async def _probe_visible_cycles():
        """Verify availability of every cycle the dialog will list.

        Runs HEAD requests in parallel (capped at 8 concurrent so we
        don't open a thundering herd of sockets). Each completed probe
        merges into cycle_check_results, triggering a re-render of the
        dialog with the verified label.
        """
        pending = [
            c for c in _BASE_TIME_CHOICES
            if c.isoformat() not in cycle_check_results
        ]
        if not pending:
            return
        sem = asyncio.Semaphore(8)

        async def one(c):
            last = (
                _SHORT_CYCLE_HORIZON_H if c.hour in _SHORT_CYCLE_HOURS
                else _LONG_CYCLE_HORIZON_H
            )
            async with sem:
                ok = await probe_cycle_complete(c, last)
            return c.isoformat(), ok

        results = await asyncio.gather(*(one(c) for c in pending))
        # Merge atomically; the existing dict carries cycles probed
        # in earlier opens of the dialog (cache survives close/reopen).
        set_cycle_check_results(
            lambda prev: {**prev, **dict(results)},
        )

    def _open_time_dialog():
        set_draft_base_choice(
            "auto" if manual_cycle is None else manual_cycle.isoformat()
        )
        set_draft_horizon_h(max_step_h)
        set_draft_cadence_h(cadence_h)
        set_show_time_dialog(True)
        # Kick off availability probes in background. Labels update
        # from "予測" to "確認済み ✓ / ✗" as each HEAD completes.
        ft.context.page.run_task(_probe_visible_cycles)

    def _commit_cycle(_):
        # ---- base time ----
        new_manual: datetime | None
        if draft_base_choice == "auto":
            new_manual = None
        else:
            try:
                new_manual = datetime.fromisoformat(draft_base_choice)
            except ValueError:
                new_manual = None
        cycle_changed = (new_manual != manual_cycle)
        # ---- horizon / cadence ----
        horizon_changed = (
            draft_horizon_h != max_step_h or draft_cadence_h != cadence_h
        )
        # Apply
        if cycle_changed:
            set_manual_cycle(new_manual)
            if new_manual is None:
                # Drop the cached holder so the next fetch re-probes.
                set_run_time_holder(None)
        if horizon_changed:
            set_max_step_h(draft_horizon_h)
            set_cadence_h(draft_cadence_h)
            # Snap step_hours to the new option set if needed.
            new_options = _compute_steps(draft_horizon_h, draft_cadence_h)
            if step_hours not in new_options and new_options:
                nearest = min(new_options, key=lambda s: abs(s - step_hours))
                set_step_hours(nearest)
        if cycle_changed or horizon_changed:
            # Different cycle or different frame set → drop in-memory PNGs.
            set_frames({})
        set_show_time_dialog(False)

    cycle_now_display = (
        manual_cycle.strftime("%Y-%m-%d %H:%M UTC") + " (manual)"
        if manual_cycle else (
            run_time_holder.strftime("%Y-%m-%d %H:%M UTC") + " (auto)"
            if run_time_holder else "未取得 / not yet probed"
        )
    )

    # Preview the frame count for the draft horizon/cadence so the user
    # can see how much they'd save before applying.
    _preview_steps = _compute_steps(draft_horizon_h, draft_cadence_h)
    _preview_frame_count = len(_preview_steps)

    # Build base-time radio rows with horizon + publication info.
    # HRES publication is atomic per cycle, so two states for the
    # predicted label (公開予定 / 公開済み(予測)). When a probe has
    # actually verified availability we promote to ✓/✗ verified text.
    def _base_time_label(dt: datetime) -> str:
        pub_at = _publication_time(dt)
        now_utc = datetime.now(tz=timezone.utc)
        verified = cycle_check_results.get(dt.isoformat())
        if verified is True:
            pub_state = f"✓ 公開確認済み ({pub_at:%m/%d %H:%M})"
        elif verified is False:
            pub_state = f"✗ 未公開 (予測 {pub_at:%m/%d %H:%M})"
        elif now_utc < pub_at:
            pub_state = f"公開予定 {pub_at:%m/%d %H:%M} (確認中…)"
        else:
            pub_state = f"公開済み 推定 ({pub_at:%m/%d %H:%M}) (確認中…)"
        horizon = _cycle_horizon_h(dt)
        if _is_short_cycle(dt):
            ext = _prior_long_cycle(dt)
            horizon_str = f"T+{horizon}h + 延長: {ext:%Hz}"
        else:
            horizon_str = f"T+{horizon}h"
        current = (
            "  ← 現在"
            if (
                manual_cycle is not None and dt == manual_cycle
            ) or (
                manual_cycle is None and run_time_holder is not None
                and dt == run_time_holder
            )
            else ""
        )
        return (
            f"{dt:%Y-%m-%d %H:%M UTC} ({dt:%Hz})  ·  "
            f"{horizon_str}  ·  {pub_state}{current}"
        )

    base_time_radios = [
        ft.Radio(
            value="auto",
            label=(
                "Auto: 全予報範囲が公開済みの最新 base time を自動選択 "
                "(常に 00z/12z を選びます; 推奨)"
            ),
        ),
    ] + [
        ft.Radio(
            value=dt.isoformat(),
            label=_base_time_label(dt),
        )
        for dt in _BASE_TIME_CHOICES
    ]

    time_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("データ / Data (base time + 範囲)"),
        content=ft.Container(
            width=560,
            height=540,
            content=ft.Column(
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.ADAPTIVE,
                controls=[
                    ft.Text(
                        "現在の base time / Current",
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        cycle_now_display,
                        size=14, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "Base time は予報の初期値時刻 (model initialization)。"
                        "00z/12z は T+240h まで、06z/18z は T+90h まで配信。"
                        "06z/18z 選択時は T+90h 以降を直前の 00z/12z で自動補完 "
                        "(stitching)。ECMWF Open Data は直近 ~5 日分のみ保持。",
                        size=10, color=ft.Colors.GREY,
                    ),
                    ft.Divider(height=10),

                    ft.Text(
                        "Base time の選択 / Choose base time",
                        size=11, color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                    ),
                    ft.RadioGroup(
                        value=draft_base_choice,
                        on_change=lambda e: set_draft_base_choice(e.control.value),
                        content=ft.Column(
                            tight=True,
                            spacing=2,
                            controls=base_time_radios,
                        ),
                    ),

                    ft.Divider(height=10),
                    ft.Text(
                        "予報範囲 / Forecast range",
                        size=11, color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "範囲を短くすると取得時間が大幅に縮みます。"
                        "粒度を粗くしてもフレーム数を減らせます。",
                        size=10, color=ft.Colors.GREY,
                    ),
                    ft.Row(
                        spacing=10,
                        controls=[
                            ft.Dropdown(
                                label="最大予報時刻 / Horizon",
                                value=str(draft_horizon_h),
                                width=200,
                                dense=True,
                                options=[
                                    ft.dropdown.Option(
                                        key=str(h),
                                        text=(
                                            f"T+{h}h"
                                            + (f" (D+{h // 24})" if h % 24 == 0 else "")
                                        ),
                                    )
                                    for h in HORIZON_CHOICES_H
                                ],
                                on_select=lambda e: set_draft_horizon_h(
                                    int(e.control.value),
                                ),
                            ),
                            ft.Dropdown(
                                label="粒度 / Cadence",
                                value=str(draft_cadence_h),
                                width=140,
                                dense=True,
                                options=[
                                    ft.dropdown.Option(
                                        key=str(c), text=f"{c}h",
                                    )
                                    for c in CADENCE_CHOICES_H
                                ],
                                on_select=lambda e: set_draft_cadence_h(
                                    int(e.control.value),
                                ),
                            ),
                        ],
                    ),
                    ft.Text(
                        f"= {_preview_frame_count} frames"
                        + (
                            "  (デフォルト 65 と同じ)"
                            if _preview_frame_count == 65 else ""
                        ),
                        size=12, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "注: ECMWF は T+144h 以降は 6h 粒度のみ。3h を指定しても"
                        "後半は 6h になります。",
                        size=10, color=ft.Colors.GREY, italic=True,
                    ),
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                "キャンセル",
                on_click=lambda _: set_show_time_dialog(False),
            ),
            ft.FilledButton("適用 / Apply", on_click=_commit_cycle),
        ],
    )

    # Show whichever dialog is open. Passing None dismisses; the hook
    # diffs the dialog dataclass field-by-field, so re-rendering with
    # updated draft state preserves cursor / focus.
    ft.use_dialog(
        catalog_dialog if show_catalog_dialog
        else layer_dialog if show_data_dialog
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
        current_idx = step_options.index(step_hours)
    except ValueError:
        current_idx = 0
    loaded_count = len(frames)
    total_count = len(step_options)
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

    # Per-step cache status for the bar in the Data section.
    # green        = rendered PNG in memory (primary cycle)
    # light green  = rendered PNG in memory (extension cycle)
    # amber        = GRIB on disk for source cycle, not yet rendered
    # grey         = nothing. We probe the SOURCE cycle for each
    #                display step so stitched frames are accounted for.
    if primary_cycle is not None and stitch_plan:
        cache_rendered = sum(1 for s in step_options if s in frames)
        cache_grib = 0
        cache_cells = []
        for display_step, src_cycle, src_step in stitch_plan:
            stitched = (src_cycle != primary_cycle)
            if display_step in frames:
                color = (
                    ft.Colors.LIGHT_GREEN if stitched else ft.Colors.GREEN
                )
            elif is_grib_cached(settings, src_cycle, src_step):
                color = ft.Colors.AMBER
                cache_grib += 1
            else:
                color = ft.Colors.OUTLINE_VARIANT
            cache_cells.append(
                ft.Container(
                    width=3, height=12, bgcolor=color, border_radius=1,
                    tooltip=(
                        f"{_step_label(display_step)}"
                        + (
                            f"  [ext {src_cycle:%Hz}]" if stitched else ""
                        )
                    ),
                )
            )
        cache_none = len(step_options) - cache_rendered - cache_grib
        stitch_note = (
            f"  | 内 {sum(1 for p in stitch_plan if p[1] != primary_cycle)} 件は "
            f"{_prior_long_cycle(primary_cycle):%Hz} 延長"
            if any(p[1] != primary_cycle for p in stitch_plan) else ""
        )
        cache_bar = ft.Column(
            spacing=2,
            controls=[
                ft.Row(spacing=1, controls=cache_cells, wrap=False),
                ft.Text(
                    f"描画 {cache_rendered} · GRIB {cache_grib} · 未取得 {cache_none}"
                    f" / 全 {len(step_options)}{stitch_note}",
                    size=10, color=ft.Colors.GREY,
                ),
            ],
        )
    else:
        cache_rendered = cache_grib = 0
        cache_none = len(step_options)
        cache_bar = ft.Text(
            "未取得 / not loaded yet",
            size=10, color=ft.Colors.GREY, italic=True,
        )

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

                # ── Data: base time + acquisition status + range ──
                ft.Divider(height=14),
                ft.Text(
                    "データ / Data (base time)", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    (
                        f"{manual_cycle:%Y-%m-%d %H:%M UTC}"
                        if manual_cycle
                        else (
                            f"{run_time_holder:%Y-%m-%d %H:%M UTC}"
                            if run_time_holder else "未取得"
                        )
                    ),
                    size=13, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "manual" if manual_cycle else "auto (latest available)",
                    size=10, color=ft.Colors.GREY,
                ),
                ft.Text(
                    (
                        f"+ {_prior_long_cycle(primary_cycle):%Hz} 延長 (stitched)"
                        if (
                            primary_cycle is not None
                            and _is_short_cycle(primary_cycle)
                            and max_step_h > _cycle_horizon_h(primary_cycle)
                        ) else ""
                    ),
                    size=10, color=ft.Colors.AMBER,
                    visible=(
                        primary_cycle is not None
                        and _is_short_cycle(primary_cycle)
                        and max_step_h > _cycle_horizon_h(primary_cycle)
                    ),
                ),
                ft.Text(
                    f"範囲: T+0..T+{MAX_STEP}h, 粒度 {cadence_h}h "
                    f"({len(step_options)} frames)",
                    size=10, color=ft.Colors.GREY,
                ),
                ft.Text(
                    "取得状況 / Cache status:",
                    size=10, color=ft.Colors.GREY,
                ),
                cache_bar,
                ft.TextButton(
                    content=ft.Text("データ変更 / Change data…", size=12),
                    icon=ft.Icons.SCHEDULE,
                    on_click=lambda _: _open_time_dialog(),
                ),

                # ── Region: geographic viewport / projection ──
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

                # ── Layer: field + rendering style ──
                ft.Divider(height=14),
                ft.Text(
                    "レイヤー / Layer", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    selected_field.bilingual_label() + selected_field.level_suffix(),
                    size=12, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    f"{selected_field.key} · {selected_field.unit}",
                    size=10, color=ft.Colors.GREY,
                ),
                ft.Text(
                    selected_field.typical_layer,
                    size=10, color=ft.Colors.GREY,
                ),
                ft.TextButton(
                    content=ft.Text("レイヤー変更 / Change layer…", size=12),
                    icon=ft.Icons.LAYERS,
                    on_click=lambda _: set_show_data_dialog(True),
                ),

                # ── Time: valid time = base time + lead time (slider-driven) ──
                ft.Divider(height=14),
                ft.Text(
                    "時間 / Time (valid time)", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    _valid_time_display(manual_cycle or run_time_holder, step_hours),
                    size=12, weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    f"Lead: {_step_label(step_hours)}",
                    size=10, color=ft.Colors.GREY,
                ),
                ft.Text(
                    f"範囲: T+0..T+{MAX_STEP}h, 3h ({total_count} frames)",
                    size=10, color=ft.Colors.GREY,
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
                                f"Cycle: {(manual_cycle or run_time_holder):%Y-%m-%d %H:%M UTC}"
                                f"{' (manual)' if manual_cycle else ' (auto)'}"
                                if (manual_cycle or run_time_holder) else "Cycle: —"
                            ),
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
