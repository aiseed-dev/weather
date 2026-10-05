---
name: data-flow
description: How GPV data, GRIB files, layers, and renders connect. Read whenever you touch ForecastRequest, ForecastService, render_pool, or any code that decides what to download or which file to open.
---

## Core idea

GPV data is **GRIB management**. Layer is **which variable to draw from
the GRIBs you already have**. Region is **which slice to project /
crop**. None of these three concerns reach into the others.

User flow consequences:

* Pressing 取得 / Fetch downloads **every implemented layer's data for
  every step of the chosen cycle, in one batch**. After that, switching
  layer or region never touches the network. Only switching cycle does.
* Switching layer redraws from the GRIBs already on disk. If the
  visible step's GRIB is on disk, the new layer renders in
  milliseconds. No fetch confirmation. No download.
* Switching region re-crops or re-reindexes the same source data; no
  network, no decode of new GRIBs.

## Concepts and the boundary between them

### GPV data layer (services/forecast_service)

* **Unit of work** = `ForecastRequest(run_time, step_hours, kind)`.
* `kind ∈ {"sfc", "pl", "sol"}` (surface, pressure level, soil layer).
  `DataField.kind` in the catalog decides which one a layer belongs to.
* What one request downloads depends on the source:
  * **ECMWF bulk sources** (`ecmwf_gcp` / `ecmwf_aws` / `ecmwf_azure` /
    `ecmwf_direct`): one HTTPS GET of the whole
    `{date}{time}-{step}h-oper-fc.grib2`, which already holds every param
    at every level. The cache file is kind-agnostic:
    `<data_dir>/ecmwf/{YYYYMMDD}/{HH}z/{step}h.grib2`. Concurrent requests
    for different kinds at the same step share one GET through a per-path
    lock. `kind` only steers the decode (`decode_kind`).
  * **Mirror** (`forecast_source = "mirror"`): one NetCDF pack per kind,
    `<data_dir>/mirror/{YYYYMMDD}/{HH}z/{step:03d}h-{kind}-core.nc`, plus
    optional `-ext*.nc` siblings when the user ticks "ext" in the fetch
    dialog.
* The module docstring of `forecast_service.py` records why bulk GET
  replaced per-param Range requests (measured RTT from Japan).
* `grib_cache_path(settings, run_time, step, kind)` and
  `is_grib_cached(...)` answer the cache question. UI code uses these
  free functions; the service's `_cache_path` delegates to the same one.
* `latest_run`, `probe_cycle_complete`, etc. stay where they are.

The forecast service knows **nothing** about LUTs, palettes, projections,
or which variable a particular layer wants. It only manages GRIBs.

### Catalog (products/catalog)

`DataField` answers two questions about a layer:

1. **Which GRIB does this layer come from?** — a `kind` property:
   `"sol"` for soil params (`sot`, `vsw`), `"pl"` if `level is not None`,
   else `"sfc"`.
2. **How do I pull this layer's array out of that GRIB?** — encoded
   in the matching `ScalarLayerConfig` (or hand-written renderer) via
   variable name + level filter.

The catalog does **not** decide download granularity. Adding a new
field just means adding one row + one config; the next fetch already
picks it up because the service downloads the whole param list for
the kind.

### Renderer (figures/*)

Per layer, a renderer takes a single GRIB file path and a region:

```
render_layer(grib_path, region, run_id, layer_key="msl", *, msl_overlay_path=None) -> bytes
```

It opens the cached file through `decode_kind`, picks its variable by
short name (`ds["msl"]`, `ds["t2m"]`, `ds["gh"].sel(isobaricInhPa=500)`,
...), applies the LUT, draws coastlines from the precomputed mask, and
encodes PNG.

Renderers are pure: same (grib, region, layer) → same bytes. The
caller decides which file to open (sfc vs pl) and whether the result
goes into the memory cache, the visible image, or both.

### UI (components/map_view)

State that drives a render:

* `primary_cycle` — selected base time. Changing it kicks the
  background precompute to fill the new cycle's frames (and triggers
  the Fetch button to reappear if the new cycle isn't cached).
* `region` — drives crop / polar reindex.
* `data_field_key` — selects which variable + level the renderer
  reads. **Never triggers a download.** If the visible step's GRIB
  for the field's kind isn't on disk, the renderer no-ops and the
  chart area shows the "press Fetch" placeholder for *that cycle*,
  not for the layer.
* `step_hours` — current frame in the timeline.

Memory cache `frames` keys on `(cycle, region, layer, overlays, step)`.
Region and layer entries coexist so toggling between recently-viewed
combos is instant.

The download loop and the background precompute are the **only**
producers of on-disk GRIB cache entries. Per step, the loop requests
each kind in `_ACTIVE_KINDS` (`sfc`, `pl`, `sol`) that isn't already
cached.

## Fetch button semantics

The Fetch button is the only path that talks to the network for
layer data. Its job per cycle:

```
plan = stitch plan for the current cycle   # (display_step, src_cycle, src_step)
for each step in plan, for each kind in _ACTIVE_KINDS:
    if not is_grib_cached(settings, src_cycle, src_step, kind):
        download(ForecastRequest(src_cycle, src_step, kind))
```

Every kind is fetched; there is no per-kind toggle. With the mirror
source, the confirm dialog offers one checkbox for the ext tier.

`更新 / Update` button is the same flow against a newer cycle.

## Open questions to confirm with the user

1. **Wind direction arrows.** The user agreed wind at pressure levels
   ships as speed-only. Surface wind10m still draws arrows. Should the
   matrix UI render a separate "風向" row alongside the "風速" wind
   chips, or stay speed-only everywhere? Suggested: keep speed-only
   for v1; revisit when we have a vector renderer that scales.

2. **MSL overlay across kinds.** The MSL contour overlay reads the sfc
   msl field. Pressure-level layers asking for it read from the same
   cached file; no separate fetch path needed. Confirm the overlay
   stays at MSL (not, say, gh@500 over t@850).

3. **Frame memory budget.** The memory cache `frames` is capped by
   `FRAMES_CACHE_LIMIT = 500` with FIFO eviction across cycles. With
   many layers × regions × steps this fills quickly. Probably want a
   per-cycle bound and to evict the previous cycle wholesale.
