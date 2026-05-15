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
import logging
import time

import flet as ft

from aiseed_weather.figures.regions import (
    GLOBAL,
    PRESETS as REGION_PRESETS,
    Region,
    by_key as region_by_key,
    custom_region,
)
from aiseed_weather.figures.render_pool import render_layer_in_pool
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
    grib_cache_path,
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

# Max distinct (cycle, region, layer, step) PNGs held in memory. At
# ~500KB per PNG this caps memory at ~250MB. Eviction is FIFO (oldest
# insertion dropped) which is a reasonable proxy for LRU here since
# the typical access pattern is "fill in time order, replay".
FRAMES_CACHE_LIMIT = 500

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


# ECMWF Open Data retention windows. Anything older is always 404 on
# the public mirror, so listing it would just waste HEAD requests and
# fill the dialog with "リテンション切れ" rows the user can't act on.
_RETENTION_LONG_DAYS = 5    # 00z / 12z (T+360h) kept ~5 days
_RETENTION_SHORT_DAYS = 3   # 06z / 18z (T+144h) kept ~3 days


def _recent_base_times():
    """Synoptic cycles within ECMWF Open Data's retention window.

    Returns cycles in descending order (newest first), filtered per
    retention so the dialog never lists a cycle we know in advance
    can no longer be downloaded. Long cycles get the longer window
    because ECMWF retains them longer.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    cycle_hour = (now.hour // 6) * 6
    latest = now.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    # Generate enough cycles to span the long retention window, then
    # filter each one against the appropriate horizon. The +1 day of
    # slack absorbs the publication lag so the very newest cycle
    # (still being published) stays on the list.
    span_cycles = (_RETENTION_LONG_DAYS + 1) * 4
    out = []
    for i in range(span_cycles):
        c = latest - timedelta(hours=6 * i)
        age_days = (now - c).total_seconds() / 86400
        limit = (
            _RETENTION_SHORT_DAYS if c.hour in _SHORT_CYCLE_HOURS
            else _RETENTION_LONG_DAYS
        )
        if age_days <= limit:
            out.append(c)
    return out


def _scan_latest_cached_cycle(settings):
    """Walk the GRIB cache and return the most recent base time that
    has at least one cached GRIB file. Used at MapView mount so the
    app comes up showing whatever the user last fetched, without
    hitting the network.
    """
    from datetime import datetime, timezone
    from aiseed_weather.models.user_settings import resolved_data_dir

    root = resolved_data_dir(settings) / "ecmwf"
    if not root.is_dir():
        return None
    latest = None
    try:
        for date_dir in root.iterdir():
            if not date_dir.is_dir():
                continue
            name = date_dir.name
            if len(name) != 8 or not name.isdigit():
                continue
            for hh_dir in date_dir.iterdir():
                if not hh_dir.is_dir():
                    continue
                if not hh_dir.name.endswith("z"):
                    continue
                hh_part = hh_dir.name[:-1]
                if not hh_part.isdigit():
                    continue
                try:
                    has_grib = any(
                        f.suffix == ".grib2" and f.stat().st_size > 0
                        for f in hh_dir.iterdir()
                    )
                except OSError:
                    continue
                if not has_grib:
                    continue
                dt = datetime.strptime(name, "%Y%m%d").replace(
                    hour=int(hh_part), tzinfo=timezone.utc,
                )
                if latest is None or dt > latest:
                    latest = dt
    except OSError:
        return None
    return latest


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
_PUBLICATION_LAG_LONG_H = 7.5   # 00z / 12z atomic publication
_PUBLICATION_LAG_SHORT_H = 6.5  # 06z / 18z atomic publication


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


@ft.component
def MapView(settings: UserSettings, fetch=None):
    # `fetch` is the FetchController from App so the download lifecycle
    # survives tab navigation. We read/mutate its observable session
    # directly — no setter plumbing. None is allowed for standalone
    # testing; in that case we synthesize a local controller. The
    # FetchController type is imported lazily to avoid a circular import
    # (app.py → map_view.py → app.py).
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error | disabled
    image_bytes, set_image_bytes = ft.use_state(None)
    error, set_error = ft.use_state(None)
    run_label, set_run_label = ft.use_state("")
    progress, set_progress = ft.use_state("")
    step_hours, set_step_hours = ft.use_state(0)

    # Rendered-PNG cache. Each render takes ~5s (cartopy + matplotlib)
    # so we keep results across region/layer/cycle changes by keying on
    # the full (cycle, region, layer, step) tuple — the same combination
    # the user might revisit later in the session is then instant.
    #
    # Bounded by FRAMES_CACHE_LIMIT entries (FIFO eviction) so memory
    # doesn't grow without limit if the user explores many regions ×
    # layers × cycles. At ~500KB per PNG, 500 entries = ~250MB.
    frames, set_frames = ft.use_state({})
    is_playing, set_is_playing = ft.use_state(False)
    # Initial base time from on-disk GRIB cache. Lets the app come
    # up on whatever the user last fetched without a network round-
    # trip, and triggers the visible-step render effect on mount.
    run_time_holder, set_run_time_holder = ft.use_state(
        lambda: _scan_latest_cached_cycle(settings)
    )
    # Stable mutable holder for the currently-scheduled animation task.
    # ft.use_state with a lazy initializer returns the same dict object
    # across renders, so setup can write the task reference and cleanup
    # can read it back even though both closures are recreated every
    # render.
    anim_task_ref, _ = ft.use_state(lambda: {"task": None})
    # Background acquisition (download) lifecycle. The download loop
    # runs independently of the view: changing region or layer does
    # NOT cancel it; only the explicit Stop button does. The shared
    # mutable cancel_event lets us tell the loop to wind down without
    # a hard cancel (which can leave half-written files).
    # Render parameters the loop should use for any frame that needs
    # rendering. Updated every render so a region/layer change is
    # picked up by the next iteration of the loop without restarting
    # the download. The loop reads these via the holder rather than
    # closure-capturing them at task launch.
    render_params_ref, _ = ft.use_state(lambda: {})

    # Standalone-mode fallback: every hook must run on every render
    # (rules of hooks), so we always declare local state/refs and only
    # bind through to App's controller if one was injected.
    from aiseed_weather.components.app import FetchSession  # local import
    local_session, _ = ft.use_state(lambda: FetchSession())
    local_task_ref = ft.use_ref(
        lambda: {"task": None, "cancel_event": None},
    )
    session = fetch.session if fetch is not None else local_session
    download_task_ref = (
        fetch.task_ref if fetch is not None else local_task_ref
    )

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

    # Overlay toggles. The base layer carries the color shading; the
    # user may stack contour / vector overlays from other fields on
    # top (Windy-style). For now only MSL isobars are supported;
    # gh@500 isohypsae and 10m wind arrows are planned.
    msl_overlay, set_msl_overlay = ft.use_state(False)

    # Fetch confirmation dialog state — surfaces from "取得開始" and
    # "更新" so the user explicitly acknowledges the (multi-minute)
    # download. Dialog body shows base time, frame count, and how
    # many are already cached.
    show_fetch_confirm, set_show_fetch_confirm = ft.use_state(False)

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

    # Refresh the render-params holder so the (independent) download
    # loop can read the latest region/layer/cycle/source on each
    # iteration without restarting.
    render_params_ref["region"] = region
    render_params_ref["data_field"] = data_field_key
    render_params_ref["primary"] = primary_cycle
    render_params_ref["source_lookup"] = source_lookup
    render_params_ref["data_source_key"] = data_source_key
    render_params_ref["msl_overlay"] = msl_overlay

    def _overlay_signature() -> tuple[str, ...]:
        """Sorted tuple of enabled overlay names, used in the cache key.

        Renders for the same (cycle, region, layer, step) with vs
        without overlays are distinct PNGs; both can coexist in the
        cache so toggling the checkbox feels instant when both have
        been generated.
        """
        active: list[str] = []
        if msl_overlay and data_field_key != "msl":
            active.append("msl")
        return tuple(sorted(active))

    def _frame_key(step, *, cycle=None, region_=None, layer=None, overlays=None):
        """Build the composite key used by the frames PNG cache.

        Defaults pull from the current MapView render scope, but
        callers (e.g. the download loop reading from render_params_ref)
        can override.
        """
        c = cycle if cycle is not None else primary_cycle
        r = region_ if region_ is not None else region
        ly = layer if layer is not None else data_field_key
        ov = overlays if overlays is not None else _overlay_signature()
        cycle_iso = c.isoformat() if c is not None else "none"
        return (cycle_iso, r.key, ly, ov, step)

    def _get_frame(step, **kw):
        return frames.get(_frame_key(step, **kw))

    def _put_frame(prev, key, png):
        new = {**prev, key: png}
        if len(new) > FRAMES_CACHE_LIMIT:
            oldest = next(iter(new))
            del new[oldest]
        return new

    async def _ensure_overlay_path(service, src_cycle, src_step, *, want_msl: bool):
        """Ensure the MSL overlay GRIB is on disk if ``want_msl`` is
        True, then return its path. Returns None if the overlay isn't
        requested, or if the base layer is already MSL (overlay would
        be redundant). On download failure we return None and let the
        renderer proceed without the overlay rather than aborting the
        whole frame.
        """
        if not want_msl:
            return None
        msl_req = ForecastRequest(
            run_time=src_cycle, step_hours=src_step, param="msl",
        )
        if not service.is_cached(msl_req):
            try:
                await service.download(msl_req)
            except Exception:
                logger.exception(
                    "MSL overlay download failed for step=%dh; "
                    "rendering without overlay", src_step,
                )
                return None
        return grib_cache_path(settings, src_cycle, src_step, param="msl")

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
            run_time=src_cycle, step_hours=src_step, param=selected_field.ecmwf_param,
        )
        # Worker process handles decode + render. Main process never
        # holds the xr.Dataset — keeps memory + GIL out of the loop.
        grib_path = await service.download(request)
        overlay_path = await _ensure_overlay_path(
            service, src_cycle, src_step,
            want_msl=msl_overlay and data_field_key != "msl",
        )
        if src_cycle == primary:
            label = f"{primary:%Y%m%d %Hz} IFS · {_step_label(display_step)}"
        else:
            label = (
                f"{primary:%Y%m%d %Hz} IFS "
                f"+ {src_cycle:%Y%m%d %Hz} ext · {_step_label(display_step)}"
            )
        return await render_layer_in_pool(
            grib_path, region_, label, data_field_key,
            msl_overlay_path=overlay_path,
        )

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
        new_run = await service.latest_run(step_hours=MAX_STEP, param=selected_field.ecmwf_param)
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
            # The frames cache is keyed by (cycle, region, layer, step)
            # so a cycle change no longer needs to invalidate it — old
            # cycle entries stay around for revisit.
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
                run_time=src_cycle, step_hours=src_step, param=selected_field.ecmwf_param,
            )
            hit_cache = service.is_cached(request)
            set_progress(
                f"Fetching MSL · {src_cycle:%Y%m%d %Hz} step={src_step}h · "
                f"{_step_label(step)}"
                f"{' (cached)' if hit_cache and not force else ''}…"
            )
            # Download the GRIB; worker process will decode + render.
            grib_path = await service.download(request, force=force)
            # Optional MSL overlay GRIB. Downloaded in the same task
            # rather than in parallel because we want to fail soft if
            # the overlay isn't available (render base anyway).
            overlay_path = await _ensure_overlay_path(
                service, run_time, step,
                want_msl=msl_overlay and data_field_key != "msl",
            )
            t_fetch = time.perf_counter()
            set_progress(f"Rendering chart ({region_used.label})…")
            png_bytes = await render_layer_in_pool(
                grib_path, region_used, label, data_field_key,
                msl_overlay_path=overlay_path,
            )
            t_render = time.perf_counter()
            t_encode = t_render  # decode + encode are folded into the worker

            set_image_bytes(png_bytes)
            set_run_label(label)
            key = _frame_key(step, cycle=run_time, region_=region_used)
            set_frames(lambda prev: _put_frame(prev, key, png_bytes))
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

    async def _render_queue(target_region_key: str, target_layer_key: str):
        """Background fill: render every step_options for the given
        (cycle, region, layer) combo from cached GRIBs.

        Only renders frames that are missing from memory and whose
        GRIB is on disk — so it costs CPU only, no network. Stops
        early if the region/layer has changed in the meantime
        (the user moved on; renders for an old combo would be wasted).
        """
        cycle = primary_cycle
        if cycle is None:
            return
        for display_step in step_options:
            # Bail if user has navigated to a different render combo.
            cur_region_key = render_params_ref.get(
                "region", region,
            ).key
            cur_layer_key = render_params_ref.get(
                "data_field", data_field_key,
            )
            if (
                cur_region_key != target_region_key
                or cur_layer_key != target_layer_key
            ):
                logger.info(
                    "Render queue stopped: region/layer changed (now %s/%s)",
                    cur_region_key, cur_layer_key,
                )
                return
            # Use the current overlay signature so we re-render
            # frames if the overlay changed even when region/layer
            # didn't.
            key = _frame_key(
                display_step, cycle=cycle,
                region_=region_by_key(target_region_key)
                    if target_region_key != "custom" else region,
                layer=target_layer_key,
            )
            if key in frames:
                continue
            await _ensure_rendered(display_step)
            # Tiny yield so the UI thread can update.
            await asyncio.sleep(0.01)

    async def _ensure_rendered(display_step: int):
        """Render one display step from cached GRIB into the frames dict.

        No-op if the frame is already rendered (frames[display_step]
        exists) or if the GRIB isn't on disk yet. Reads the current
        region/layer from render_params_ref so a region change between
        when this was scheduled and when it runs uses the latest.
        Updates image_bytes + run_label only if display_step matches
        the currently-visible step.
        """
        cur_region = render_params_ref.get("region", region)
        cur_primary = render_params_ref.get("primary", primary_cycle)
        cur_layer = render_params_ref.get("data_field", data_field_key)
        cur_lookup = render_params_ref.get("source_lookup", source_lookup)
        cur_source = render_params_ref.get("data_source_key", data_source_key)
        cur_overlay = render_params_ref.get("msl_overlay", msl_overlay)
        want_msl_overlay = bool(cur_overlay) and cur_layer != "msl"
        # Resolve the field from cur_layer rather than the outer-scope
        # selected_field. _ensure_rendered runs as a background task,
        # and its closure can outlive the render that created it — so a
        # layer switch between "render scheduled" and "render runs" would
        # otherwise hand the worker an MSL GRIB while telling it to draw
        # T2m (or vice versa).
        cur_field = field_by_key(cur_layer)
        cur_overlay_sig = ("msl",) if want_msl_overlay else ()
        cache_key = _frame_key(
            display_step, cycle=cur_primary, region_=cur_region,
            layer=cur_layer, overlays=cur_overlay_sig,
        )
        if cache_key in frames:
            # Still serve it as the visible image if it matches.
            if display_step == step_hours:
                set_image_bytes(frames[cache_key])
            return
        src_cycle, src_step = cur_lookup.get(
            display_step, (cur_primary, display_step),
        )
        if src_cycle is None or not is_grib_cached(settings, src_cycle, src_step, param=cur_field.ecmwf_param):
            return  # GRIB not yet downloaded
        try:
            service = ForecastService(settings, override_source=cur_source)
        except ForecastDisabledError:
            return
        if cur_primary is not None and src_cycle == cur_primary:
            label = (
                f"{cur_primary:%Y%m%d %Hz} IFS · {_step_label(display_step)}"
            )
        else:
            label = (
                f"{cur_primary:%Y%m%d %Hz} IFS "
                f"+ {src_cycle:%Y%m%d %Hz} ext · {_step_label(display_step)}"
            )
        # Worker process decodes + renders. We just hand it the path.
        gpath = grib_cache_path(settings, src_cycle, src_step, param=cur_field.ecmwf_param)
        # Overlay GRIB path — must be cached already; we don't kick off
        # a download here because _ensure_rendered runs in latency-
        # sensitive paths (slider scrubbing, animation). If not cached,
        # render the base only and let the background loop pick it up.
        overlay_path = None
        if want_msl_overlay and is_grib_cached(
            settings, src_cycle, src_step, param="msl"
        ):
            overlay_path = grib_cache_path(
                settings, src_cycle, src_step, param="msl",
            )
        # Visible step about to render: transition state so the main
        # area drops the "press 取得 / Fetch" idle placeholder and shows
        # a spinner. Without this, the user sees the misleading idle
        # placeholder for the entire 5-15s cartopy render — even though
        # the GRIB is on disk and we are actually rendering from cache.
        if display_step == step_hours:
            set_state("loading")
            set_progress("キャッシュ済データから描画中… / Rendering from cache…")
        try:
            png = await render_layer_in_pool(
                gpath, cur_region, label, cur_layer,
                msl_overlay_path=overlay_path,
            )
        except Exception:
            logger.exception("Render of step=%dh failed", display_step)
            if display_step == step_hours:
                # Don't strand the user on a spinner. Roll back to idle
                # so the placeholder appears again; an error message is
                # surfaced via the logger.
                set_state("idle")
                set_progress("")
            return
        # Functional update with FIFO bound so we don't clobber
        # concurrent renders or grow without limit.
        set_frames(lambda prev: _put_frame(prev, cache_key, png))
        # Only swap the visible image if this is still the current step.
        if display_step == step_hours:
            set_image_bytes(png)
            set_run_label(label)
            set_progress("")
            # _ensure_rendered runs as a background task and is the only
            # path that paints a PNG when a download finishes the visible
            # step. Move out of "loading" so the main area shows the chart.
            set_state("ready")

    async def _download_loop(cycle, plan, cancel_event):
        """Pure background download. No rendering inside the loop —
        rendering is on demand via _ensure_rendered. Region/layer
        changes during the loop don't restart it.

        Maintains a per-frame ``items`` list in the App-level fetch
        session so the Fetch tab can render pip install-style detail
        (status / size / duration per row).
        """
        session.running = True
        set_error(None)
        # Seed the items list — one row per plan entry, all "pending"
        # until the loop visits them. Whole-list assign notifies the
        # observable; the framework auto-wraps the new list so later
        # in-place mutations also notify.
        session.items = [
            {
                "step": disp,
                "param": selected_field.ecmwf_param,
                "stitched": (src_c != cycle),
                "status": "pending",
                "size_bytes": None,
                "duration_s": None,
            }
            for (disp, src_c, _src_s) in plan
        ]
        items = session.items

        def _push():
            # In-place item dict mutations don't bubble through the
            # parent ``items`` list's notification, so re-assign the
            # list to fire a single coarse-grained re-render. Cheap
            # because the elements are dicts — only the list wrapper
            # is rebuilt.
            session.items = list(items)

        try:
            service = ForecastService(settings, override_source=data_source_key)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            session.running = False
            return
        total = len(plan)
        session.progress = {"done": 0, "total": total}
        try:
            for i, (display_step, src_cycle, src_step) in enumerate(plan):
                if cancel_event.is_set():
                    logger.info(
                        "Download loop cancelled at frame %d/%d", i, total,
                    )
                    for it in items[i:]:
                        it["status"] = "cancelled"
                    _push()
                    break
                req = ForecastRequest(
                    run_time=src_cycle, step_hours=src_step,
                    param=selected_field.ecmwf_param,
                )
                ext_tag = (
                    f" [ext {src_cycle:%Hz}]" if src_cycle != cycle else ""
                )
                items[i]["status"] = "checking"
                _push()
                hit_cache = service.is_cached(req)
                if hit_cache:
                    msg = (
                        f"Cache check {i + 1}/{total} · "
                        f"{_step_label(display_step)}{ext_tag}"
                    )
                    set_progress(msg)
                    session.status_text = msg
                    try:
                        items[i]["size_bytes"] = (
                            service._cache_path(req).stat().st_size
                        )
                    except OSError:
                        pass
                    items[i]["status"] = "cached"
                    items[i]["duration_s"] = 0.0
                    _push()
                else:
                    msg = (
                        f"DL {i + 1}/{total} · "
                        f"{_step_label(display_step)}{ext_tag}…"
                    )
                    set_progress(msg)
                    session.status_text = msg
                    items[i]["status"] = "downloading"
                    _push()
                    t0 = time.perf_counter()
                    try:
                        await service.download(req)
                    except Exception:
                        logger.exception(
                            "Download of step=%dh (src=%s/%dh) failed",
                            display_step, src_cycle, src_step,
                        )
                        items[i]["status"] = "failed"
                        _push()
                        continue
                    items[i]["duration_s"] = time.perf_counter() - t0
                    try:
                        items[i]["size_bytes"] = (
                            service._cache_path(req).stat().st_size
                        )
                    except OSError:
                        pass
                    items[i]["status"] = "done"
                    _push()
                    if not cancel_event.is_set() and i + 1 < total:
                        await asyncio.sleep(PRELOAD_SPACING_SEC)

                # MSL overlay companion download (when user has the
                # toggle on and the base isn't already MSL). We don't
                # add a separate row for this — it's the same display
                # step, and the overlay file's bytes are added into
                # the same item's size_bytes accumulator.
                want_overlay = (
                    render_params_ref.get("msl_overlay", msl_overlay)
                    and render_params_ref.get(
                        "data_field", data_field_key,
                    ) != "msl"
                )
                if want_overlay and not cancel_event.is_set():
                    msl_req = ForecastRequest(
                        run_time=src_cycle, step_hours=src_step, param="msl",
                    )
                    if not service.is_cached(msl_req):
                        try:
                            await service.download(msl_req)
                            try:
                                extra = service._cache_path(msl_req).stat().st_size
                                items[i]["size_bytes"] = (
                                    items[i].get("size_bytes") or 0
                                ) + extra
                                _push()
                            except OSError:
                                pass
                            if not cancel_event.is_set() and i + 1 < total:
                                await asyncio.sleep(PRELOAD_SPACING_SEC)
                        except Exception:
                            logger.exception(
                                "MSL overlay DL failed for step=%dh; "
                                "rendering will skip overlay for this frame",
                                src_step,
                            )

                # Whole-dict assign rather than mutating session.progress
                # in place: in-place dict mutation does notify, but
                # whole-replace is more readable and the dict is tiny.
                session.progress = {**session.progress, "done": i + 1}
                # Render every frame as soon as its GRIB lands. The
                # visible step is awaited so the user sees their selected
                # frame come alive immediately; non-visible steps are
                # fire-and-forget so the download loop keeps moving while
                # the render pool serialises the actual work in parallel
                # across CPU cores. Without this, only the visible step
                # ever rendered and "描画 1 · GRIB 64" stayed stuck.
                if display_step == step_hours:
                    await _ensure_rendered(display_step)
                else:
                    ft.context.page.run_task(_ensure_rendered, display_step)
        finally:
            session.running = False
            set_progress("")
            session.status_text = ""

    def start_download():
        # Cancel any previous download first.
        stop_download()
        if primary_cycle is None:
            # We don't have a cycle yet — kick off probe + download.
            ft.context.page.run_task(_probe_then_download)
            return
        cancel_event = asyncio.Event()
        plan = _stitch_plan(primary_cycle, max_step_h, cadence_h)
        task = ft.context.page.run_task(
            _download_loop, primary_cycle, plan, cancel_event,
        )
        download_task_ref.current["task"] = task
        download_task_ref.current["cancel_event"] = cancel_event

    def stop_download():
        # Defer to the App-level stop if we have a controller, so the
        # global banner clears in lockstep. Falls through to local
        # stop logic when no controller (tests / standalone).
        if fetch is not None:
            fetch.stop()
            return
        ev = download_task_ref.current.get("cancel_event")
        if ev is not None:
            ev.set()
        task = download_task_ref.current.get("task")
        if task is not None and not task.done():
            task.cancel()
        download_task_ref.current["task"] = None
        download_task_ref.current["cancel_event"] = None
        session.running = False

    async def _probe_then_download():
        try:
            service = ForecastService(settings, override_source=data_source_key)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            return
        run_time, _ = await _resolve_run_time(service, force=False)
        set_run_time_holder(run_time)
        # Build plan against the just-probed cycle (render-scope plan
        # was empty until run_time_holder was set).
        plan = _stitch_plan(run_time, max_step_h, cadence_h)
        cancel_event = asyncio.Event()
        download_task_ref.current["cancel_event"] = cancel_event
        download_task_ref.current["task"] = None
        await _download_loop(run_time, plan, cancel_event)

    async def load_all_steps_and_play():
        """Compatibility shim for the old ▶ button: start the download
        + flip is_playing once the first frame is rendered. The animation
        engine plays through whatever frames are rendered at the time
        of each tick — frames not yet ready are skipped silently.
        """
        set_is_playing(False)
        start_download()
        # Wait briefly for the first frame to land, then start playing.
        # If the user's selected step isn't downloaded yet, we'll still
        # start the loop; the tick will just stall on missing frames.
        for _ in range(40):  # up to ~10s
            if _get_frame(step_hours) is not None or session.progress.get("done", 0) > 0:
                break
            await asyncio.sleep(0.25)
        set_is_playing(True)

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
        next_png = _get_frame(next_step)
        if next_png is None:
            # Missing frame — try lazy render then carry on; if still
            # nothing, advance silently (don't stall the whole loop).
            await _ensure_rendered(next_step)
            next_png = _get_frame(next_step)
        set_step_hours(next_step)
        if next_png is not None:
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

    # When the visible step or the render params change, ensure the
    # currently-displayed frame matches. Region/layer change ↔ frames
    # dict cleared elsewhere; this kicks the rendering for the visible
    # step from disk if it's there. No-op if the GRIB isn't downloaded.
    def _maybe_render_visible():
        ft.context.page.run_task(_ensure_rendered, step_hours)

    ft.use_effect(
        _maybe_render_visible,
        [step_hours, region, data_field_key, primary_cycle],
    )

    # On mount: probe only the most recent few cycles so the bootstrap
    # can lock onto the latest published base time. Full dialog probe
    # (every row) is deferred until the user opens the GPV dialog.
    def _probe_on_mount():
        ft.context.page.run_task(_probe_latest_cycle)
    ft.use_effect(_probe_on_mount, [])

    # First-run bootstrap: if the disk had no cache to load and the
    # mount-time probe has landed at least one verified cycle, set
    # run_time_holder to the newest verified cycle. Short cycles
    # (06z/18z, T+144h) are fine here — the stitch_plan transparently
    # extends past their horizon by pulling from the prior 00z/12z.
    def _bootstrap_run_time_from_probe():
        if run_time_holder is not None:
            return
        if not cycle_check_results:
            return
        from datetime import datetime as _dt
        verified = []
        for iso, ok in cycle_check_results.items():
            if not ok:
                continue
            try:
                verified.append(_dt.fromisoformat(iso))
            except ValueError:
                pass
        if verified:
            set_run_time_holder(max(verified))
    ft.use_effect(
        _bootstrap_run_time_from_probe,
        [cycle_check_results, run_time_holder],
    )

    def handle_slider_change(e):
        idx = int(e.control.value)
        new_step = step_options[idx]
        if new_step == step_hours:
            return
        set_is_playing(False)
        # _maybe_render_visible (effect on step_hours) will load the
        # cached PNG or kick a render. We just update step + label.
        set_step_hours(new_step)
        cached = _get_frame(new_step)
        if cached is not None:
            set_image_bytes(cached)
        set_run_label(
            f"{run_time_holder:%Y%m%d %Hz} IFS · {_step_label(new_step)}"
            if run_time_holder else _step_label(new_step)
        )

    def step_by(delta: int):
        idx = step_options.index(step_hours)
        new_step = step_options[(idx + delta) % len(step_options)]
        set_is_playing(False)
        set_step_hours(new_step)
        cached = _get_frame(new_step)
        if cached is not None:
            set_image_bytes(cached)
        set_run_label(
            f"{run_time_holder:%Y%m%d %Hz} IFS · {_step_label(new_step)}"
            if run_time_holder else _step_label(new_step)
        )

    def apply_region(new_region: Region):
        # Region change does NOT clear the frame cache (entries are
        # keyed by region, so old-region renders stay reachable for
        # instant switch-back). The visible step is re-rendered for
        # the new region by _maybe_render_visible. Other steps are
        # filled in by the background re-render queue if needed.
        if new_region == region:
            set_show_region_dialog(False)
            return
        set_region(new_region)
        set_show_region_dialog(False)
        # Background fill: render all step_options for the new region
        # from cached GRIBs. Skips frames already in the memory cache.
        if primary_cycle is not None:
            ft.context.page.run_task(_render_queue, new_region.key, data_field_key)

    def toggle_play(_):
        if is_playing:
            set_is_playing(False)
            return
        # Missing means "not in memory cache for current view".
        missing = [s for s in step_options if _get_frame(s) is None]
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
            # The frames cache is keyed by (cycle, region, layer, step)
            # — products don't affect cache validity directly. Future
            # work: if products start providing different fields, key
            # by product too.
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
        title=ft.Text("気象モデル選択 / Select weather model"),
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
            # Cache is keyed by layer, so old-layer renders stay
            # cached for instant switch-back. Background fill the
            # new layer for current region+cycle.
            if primary_cycle is not None:
                ft.context.page.run_task(_render_queue, region.key, key)
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
                        f"気象モデル: {selected_product.bilingual_label()}",
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

    _BASE_TIME_CHOICES = _recent_base_times()
    _current_base_iso = (
        manual_cycle.isoformat() if manual_cycle is not None
        else (run_time_holder.isoformat() if run_time_holder is not None else "auto")
    )

    draft_base_choice, set_draft_base_choice = ft.use_state(
        "auto" if manual_cycle is None else manual_cycle.isoformat()
    )
    draft_horizon_h, set_draft_horizon_h = ft.use_state(max_step_h)
    draft_cadence_h, set_draft_cadence_h = ft.use_state(cadence_h)

    async def _probe_cycle(c):
        """HEAD one cycle, merge result into cycle_check_results."""
        last = (
            _SHORT_CYCLE_HORIZON_H if c.hour in _SHORT_CYCLE_HOURS
            else _LONG_CYCLE_HORIZON_H
        )
        ok = await probe_cycle_complete(c, last)
        set_cycle_check_results(
            lambda prev, k=c.isoformat(), v=ok: {**prev, k: v},
        )

    async def _probe_latest_cycle():
        """Find the most recent published cycle with a tight probe.

        Walks back from the newest theoretical cycle, stopping at
        the first verified hit. Bounded at 4 attempts (≈ one day of
        cycles) — past that we'd be looking at retention-aged
        cycles and the disk cache fallback handles it just fine.
        Single HEAD per attempt, sequential because we want to stop
        at the first hit, not race them all.
        """
        candidates = [
            c for c in _BASE_TIME_CHOICES[:4]
            if c.isoformat() not in cycle_check_results
        ]
        for c in candidates:
            await _probe_cycle(c)
            # Look up our own write — set_state is async wrt render
            # so we can't read cycle_check_results here. Probe the
            # next anyway if this one failed; the caller bound at 4
            # caps the worst case.

    async def _probe_visible_cycles():
        """Verify availability of every cycle the dialog will list.

        Called when the dialog opens — the user can see verification
        state inline against each row. Capped at 8 concurrent HEADs
        so we don't open a thundering herd. Mount-time probes go
        through _probe_latest_cycle, not this, so cold launches stay
        cheap.
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
        # Cache is keyed by cycle so old-cycle renders persist for
        # instant switch-back. Horizon/cadence change just re-derives
        # step_options; old PNGs that match the new step keys still
        # work, others sit dormant.
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
        # ECMWF Open Data retention is asymmetric: long cycles
        # (00z/12z) stay for ~5 days, short cycles (06z/18z) drop off
        # at ~3 days. A 404 therefore means one of two very different
        # things — "not published yet" vs "aged out of retention" —
        # and the label has to distinguish them so the user doesn't
        # try to refetch a cycle that no longer exists upstream.
        if verified is True:
            pub_state = f"✓ 公開確認済み ({pub_at:%m/%d %H:%M})"
        elif verified is False:
            if now_utc < pub_at:
                pub_state = f"✗ 未公開 (予測 {pub_at:%m/%d %H:%M})"
            else:
                pub_state = (
                    f"✗ リテンション切れ (公開: {pub_at:%m/%d %H:%M})"
                )
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
                "Auto: 公開済みの最新 base time を自動選択 "
                "(06z/18z の場合は前 00z/12z で T+144h 以降を延長; 推奨)"
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
        title=ft.Text("GPV データ / GPV (base time + range)"),
        content=ft.Container(
            width=560,
            height=540,
            content=ft.Column(
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.ADAPTIVE,
                controls=[
                    ft.Text(
                        "現在の GPV (base time)",
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

    # ----- Fetch confirmation dialog -----
    # Tally how many of the requested frames are already on disk for
    # this (cycle, layer) combo. Shown in the dialog so the user can
    # see at a glance how much work the fetch will actually do.
    if primary_cycle is not None and stitch_plan:
        already_cached_count = sum(
            1 for (_disp, src_c, src_s) in stitch_plan
            if is_grib_cached(
                settings, src_c, src_s,
                param=selected_field.ecmwf_param,
            )
        )
        to_fetch_count = len(stitch_plan) - already_cached_count
    else:
        already_cached_count = 0
        to_fetch_count = len(step_options)

    def _confirm_fetch(_):
        set_show_fetch_confirm(False)
        # Move out of "idle" right away so the main area swaps the
        # "Press 取得" placeholder for the loading spinner. The
        # download loop runs in the background and won't itself
        # touch state — _ensure_rendered promotes to "ready" once
        # the visible frame is painted.
        if state == "idle":
            set_state("loading")
        start_download()

    fetch_confirm_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("GPV データ取得 / Fetch GPV"),
        content=ft.Container(
            width=440,
            content=ft.Column(
                tight=True,
                spacing=8,
                controls=[
                    ft.Text(
                        f"Base time: "
                        f"{primary_cycle:%Y-%m-%d %H:%M UTC}"
                        if primary_cycle else "Base time: 未取得",
                        size=14, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        f"気象モデル: {selected_product.bilingual_label()}",
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        f"レイヤー: {selected_field.bilingual_label()}"
                        f"{selected_field.level_suffix()}"
                        + (" + MSL 等圧線" if msl_overlay and data_field_key != "msl" else ""),
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Text(
                        f"範囲: T+0..T+{MAX_STEP}h, 粒度 {cadence_h}h "
                        f"({len(step_options)} frames)",
                        size=11, color=ft.Colors.GREY,
                    ),
                    ft.Divider(height=8),
                    ft.Text(
                        f"取得済: {already_cached_count} / {len(step_options)} "
                        f"frames  ·  新規取得: {to_fetch_count}",
                        size=12, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "推定取得時間: 1 frame あたり ~5-10 秒。"
                        "並列ワーカーで分散実行。バックグラウンドで"
                        "進行し、いつでも停止できます。",
                        size=10, color=ft.Colors.GREY,
                    ),
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                "キャンセル",
                on_click=lambda _: set_show_fetch_confirm(False),
            ),
            ft.FilledButton(
                "取得開始 / Start fetch",
                icon=ft.Icons.DOWNLOAD,
                on_click=_confirm_fetch,
                disabled=(primary_cycle is None or to_fetch_count == 0),
            ),
        ],
    )

    # One use_dialog hook per dialog. The 0.85 hook tracks each call
    # site independently, frozen-diffs the dataclass field-by-field on
    # re-renders, and only emits the actual deltas — which is what
    # keeps text-field cursor / focus / selection alive across draft
    # state edits. A single cascading hook would defeat that because
    # the diff target changes wholesale when the active dialog flips.
    ft.use_dialog(catalog_dialog if show_catalog_dialog else None)
    ft.use_dialog(layer_dialog if show_data_dialog else None)
    ft.use_dialog(region_dialog if show_region_dialog else None)
    ft.use_dialog(time_dialog if show_time_dialog else None)
    ft.use_dialog(fetch_confirm_dialog if show_fetch_confirm else None)

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
    loaded_count = sum(1 for s in step_options if _get_frame(s) is not None)
    total_count = len(step_options)
    all_loaded = loaded_count >= total_count
    has_image = image_bytes is not None
    is_working = bool(progress)
    play_icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
    play_tooltip = "一時停止" if is_playing else (
        "▶ アニメーション再生" if all_loaded
        else f"全フレームを読み込んで再生 ({loaded_count}/{total_count})"
    )

    # (Left panel previously had a "取得 / 再取得 / 停止" button block
    # at the bottom. Removed per user direction: the fetch lifecycle
    # is now driven from the GPV card ("GPV データ変更…" → confirm
    # dialog) and the bottom panel ("停止 / Stop" in the tab header),
    # so the per-view duplicate was just noise.)

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

    # (Status badge for the panel-displayed weather model was removed:
    # only implemented models are ever displayed there, so the badge
    # would always read "wired" — redundant.)

    # Newer-cycle detection: any cycle the mount-time probe verified
    # as published, strictly newer than what we're currently showing.
    # Triggers the "更新 / Update" button in the GPV card. Short
    # cycles (06z/18z) qualify too — stitching extends them past
    # T+144h via the prior 00z/12z when the user crosses the boundary.
    from datetime import datetime as _dt
    newer_cycle = None
    if run_time_holder is not None and cycle_check_results:
        verified_newer = []
        for iso, ok in cycle_check_results.items():
            if not ok:
                continue
            try:
                c = _dt.fromisoformat(iso)
            except ValueError:
                continue
            if c > run_time_holder:
                verified_newer.append(c)
        if verified_newer:
            newer_cycle = max(verified_newer)

    def _apply_newer_cycle(target):
        # Switch the auto-locked cycle to the newer one and then ask
        # the user to confirm download (which takes minutes).
        set_run_time_holder(target)
        set_manual_cycle(None)
        # Drop visible image so the user sees we're on a new cycle
        # with no data yet. The new cycle's GPV card is the focus.
        set_image_bytes(None)
        set_show_fetch_confirm(True)

    def _request_fetch():
        """User-facing entry point for starting a fetch.

        Routes through fetch_confirm_dialog so the user understands
        they're about to commit minutes of background download. The
        dialog body lists the base time / model / layer / frame count
        / how many of those frames are already on disk.
        """
        if primary_cycle is None:
            # No probed cycle yet — fall back to direct probe+DL kick.
            start_download()
            return
        set_show_fetch_confirm(True)

    # Per-step cache status for the bar in the Data section.
    # green        = rendered PNG in memory (primary cycle)
    # light green  = rendered PNG in memory (extension cycle)
    # amber        = GRIB on disk for source cycle, not yet rendered
    # grey         = nothing. We probe the SOURCE cycle for each
    #                display step so stitched frames are accounted for.
    if primary_cycle is not None and stitch_plan:
        cache_rendered = sum(
            1 for s in step_options if _get_frame(s) is not None
        )
        cache_grib = 0
        cache_cells = []
        for display_step, src_cycle, src_step in stitch_plan:
            stitched = (src_cycle != primary_cycle)
            if _get_frame(display_step) is not None:
                color = (
                    ft.Colors.LIGHT_GREEN if stitched else ft.Colors.GREEN
                )
            elif is_grib_cached(settings, src_cycle, src_step, param=selected_field.ecmwf_param):
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
                # ───────────────────────────────────────────────
                # 1. 気象モデル / Weather model
                # Pick this first — base time is meaningless without
                # knowing which model's cycle we're talking about.
                # Only implemented models are ever displayed here, so
                # no status badge is needed (would always be "wired").
                # Visually understated because most users settle on
                # one model and rarely change.
                # ───────────────────────────────────────────────
                ft.Text(
                    "気象モデル / Weather model", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Row(
                    spacing=4,
                    controls=[
                        ft.Text(
                            selected_product.display_name(),
                            size=12, weight=ft.FontWeight.BOLD,
                            expand=True,
                        ),
                        # Hover for spec / agency / backend / license
                        # so the panel stays clean.
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.INFO_OUTLINE,
                                size=14, color=ft.Colors.GREY,
                            ),
                            tooltip=(
                                f"{selected_product.bilingual_label()}\n"
                                f"{selected_product.spec}\n"
                                f"{selected_product.agency} · "
                                f"{selected_product.backend}\n"
                                f"License: {selected_product.license_info}"
                            ),
                        ),
                    ],
                ),
                ft.TextButton(
                    content=ft.Text("モデル変更 / Change…", size=12),
                    icon=ft.Icons.LIST_ALT,
                    on_click=lambda _: set_show_catalog_dialog(True),
                ),

                # ───────────────────────────────────────────────
                # 2. GPV データ (base time) — THE primary action.
                # Commercial weather apps hide this in their backend
                # ("always latest"); we expose it because the whole
                # point of this app is to give the expert user direct
                # control over which forecast cycle they're reading.
                # Primary-coloured card so the eye lands here.
                # ───────────────────────────────────────────────
                ft.Divider(height=14),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                    border_radius=8,
                    bgcolor=ft.Colors.PRIMARY_CONTAINER,
                    content=ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text(
                                "GPV データ / GPV (base time)",
                                size=12, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
                            ),
                            ft.Text(
                                (
                                    f"{manual_cycle:%Y-%m-%d %H:%M UTC}"
                                    if manual_cycle
                                    else (
                                        f"{run_time_holder:%Y-%m-%d %H:%M UTC}"
                                        if run_time_holder else "未取得 / not loaded"
                                    )
                                ),
                                size=16, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
                            ),
                            ft.Text(
                                "manual" if manual_cycle else "auto (latest available)",
                                size=10,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
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
                                size=10, color=ft.Colors.AMBER_900,
                                visible=(
                                    primary_cycle is not None
                                    and _is_short_cycle(primary_cycle)
                                    and max_step_h > _cycle_horizon_h(primary_cycle)
                                ),
                            ),
                            ft.Text(
                                f"範囲: T+0..T+{MAX_STEP}h, 粒度 {cadence_h}h "
                                f"({len(step_options)} frames)",
                                size=10,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
                            ),
                        ],
                    ),
                ),
                ft.Text(
                    "取得状況 / Cache status:",
                    size=10, color=ft.Colors.GREY,
                ),
                cache_bar,
                # Primary fetch action: kicks the user into the fetch
                # confirmation dialog. Shown whenever there are frames
                # still to download for the current cycle and a download
                # isn't already in flight. Once everything is on disk
                # (cache_none == 0) we hide it — re-fetching the same
                # immutable cycle is pointless; the user switches cycle
                # via 更新 (newer cycle) or GPV データ変更.
                ft.FilledButton(
                    content=ft.Text("取得 / Fetch", size=12),
                    icon=ft.Icons.CLOUD_DOWNLOAD,
                    on_click=lambda _: _request_fetch(),
                    disabled=(
                        primary_cycle is None or session.running
                    ),
                    visible=(
                        not session.running
                        and (primary_cycle is None or cache_none > 0)
                    ),
                ),
                # All frames already on disk for this cycle. Surface a
                # quiet "✓ キャッシュ済" indicator instead of the Fetch
                # button so the user can see the current cycle is fully
                # downloaded without the button shouting at them.
                ft.Row(
                    spacing=4,
                    visible=(
                        not session.running
                        and primary_cycle is not None
                        and cache_none == 0
                    ),
                    controls=[
                        ft.Icon(
                            ft.Icons.CHECK_CIRCLE,
                            size=14, color=ft.Colors.GREEN,
                        ),
                        ft.Text(
                            f"キャッシュ済 / Cached · {len(step_options)} frames",
                            size=11, color=ft.Colors.GREEN,
                        ),
                    ],
                ),
                # When the mount-time probe found a newer fully-
                # published cycle than what we're showing, surface a
                # one-click switch. Hidden otherwise.
                ft.FilledTonalButton(
                    content=ft.Text(
                        (
                            f"更新 / Update → {newer_cycle:%m-%d %Hz}"
                            if newer_cycle else ""
                        ),
                        size=12,
                    ),
                    icon=ft.Icons.NEW_RELEASES,
                    visible=(newer_cycle is not None),
                    on_click=(
                        (lambda _: _apply_newer_cycle(newer_cycle))
                        if newer_cycle else None
                    ),
                ),
                ft.TextButton(
                    content=ft.Text("GPV データ変更 / Change GPV…", size=12),
                    icon=ft.Icons.SCHEDULE,
                    on_click=lambda _: _open_time_dialog(),
                ),

                # ───────────────────────────────────────────────
                # 3+. Display configuration. Less prominent because
                # once set the user rarely changes these mid-session.
                # ───────────────────────────────────────────────

                # ── Layer: field + rendering style ──
                ft.Divider(height=14),
                ft.Text(
                    "レイヤー / Layer", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Row(
                    spacing=4,
                    controls=[
                        ft.Text(
                            selected_field.bilingual_label()
                            + selected_field.level_suffix(),
                            size=12, weight=ft.FontWeight.BOLD,
                            expand=True,
                        ),
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.INFO_OUTLINE,
                                size=14, color=ft.Colors.GREY,
                            ),
                            tooltip=(
                                f"{selected_field.key} · "
                                f"{selected_field.unit}\n"
                                f"{selected_field.typical_layer}"
                            ),
                        ),
                    ],
                ),
                ft.TextButton(
                    content=ft.Text("レイヤー変更 / Change layer…", size=12),
                    icon=ft.Icons.LAYERS,
                    on_click=lambda _: set_show_data_dialog(True),
                ),

                # ── Overlay: optional contour/vector on top of the base ──
                ft.Divider(height=14),
                ft.Text(
                    "オーバーレイ / Overlay", size=11,
                    color=ft.Colors.GREY, weight=ft.FontWeight.BOLD,
                ),
                ft.Checkbox(
                    label="海面気圧 (MSL) 等圧線",
                    value=msl_overlay,
                    disabled=(data_field_key == "msl"),
                    on_change=lambda e: set_msl_overlay(bool(e.control.value)),
                ),
                ft.Text(
                    "(将来: 500hPa 等高度線、10m風矢印)",
                    size=10, color=ft.Colors.GREY, italic=True,
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

                # ── Time: valid time = base time + lead time (slider-driven) ──
                ft.Divider(height=14),
                ft.Text(
                    "時間 / Time (valid time = base + lead)", size=11,
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
            ],
        ),
    )

    # ----- Main: chart area depending on state -----
    if state == "idle":
        # Context-aware placeholder. When GRIBs are already on disk for
        # this cycle, the user has nothing left to do — they're waiting
        # for a render. When nothing is on disk, point them at Fetch.
        any_cached = (
            primary_cycle is not None
            and cache_none < len(step_options)
        )
        idle_message = (
            "キャッシュ済 GPV から描画準備中…\n"
            "(初回は cartopy + matplotlib のインポートで数秒〜十数秒)"
            if any_cached
            else "左側 GPV カードの「取得 / Fetch」を押してください。"
        )
        idle_controls = [
            ft.Icon(
                ft.Icons.PUBLIC, size=64, color=ft.Colors.OUTLINE_VARIANT,
            ),
        ]
        if any_cached:
            idle_controls.append(ft.ProgressRing(width=24, height=24))
        idle_controls.append(
            ft.Text(
                idle_message,
                size=14, color=ft.Colors.GREY,
                text_align=ft.TextAlign.CENTER,
            ),
        )
        main_area = ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                controls=idle_controls,
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
