---
name: data-flow
description: How GPV data, GRIB files, layers, and renders connect. Read whenever you touch ForecastRequest, ForecastService, render_pool, or any code that decides what to download or which file to open. The current code does NOT match this design yet — see the migration table at the bottom.
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
* `kind ∈ {"sfc", "pl"}`. ECMWF Open Data forbids mixing surface and
  pressure-level fields in one request, so we always issue at most one
  request per kind per step.
* Each request returns **one multi-band GRIB**:
  * `sfc` GRIB contains all implemented surface params at this step
    (msl, 2t, 2d, skt, sd, tcc, 10u, 10v, tp, ...).
  * `pl` GRIB contains all implemented pressure-level params crossed
    with all levels we care about (gh / t / u / v / w / r × 200 / 250
    / 300 / 500 / 700 / 850 / 925 hPa).
* Cache file naming: `<step>h-sfc.grib2` and `<step>h-pl.grib2` under
  `<cycle-dir>`. **No param in the filename.** Layer churn never
  invalidates a cache entry.
* `is_grib_cached(cycle, step, kind)` answers the cache question.
* `latest_run`, `probe_cycle_complete`, etc. stay where they are.

The forecast service knows **nothing** about LUTs, palettes, projections,
or which variable a particular layer wants. It only manages GRIBs.

### Catalog (products/catalog)

`DataField` answers two questions about a layer:

1. **Which GRIB does this layer come from?** — a `kind` property
   derived from `level`: `"pl"` if `level is not None`, else `"sfc"`.
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
render_layer(grib_path, region, run_id, layer_key) -> bytes
```

It opens the multi-band GRIB, picks its variable by short name
(`ds["msl"]`, `ds["t2m"]`, `ds["gh"].sel(isobaricInhPa=500)`, ...),
applies the LUT, draws coastlines from the precomputed mask, encodes
PNG. Same fast pipeline as today.

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
producers of on-disk GRIB cache entries. Both honour the `kind`
split: per step, the loop fetches whichever of {sfc, pl} are needed
and aren't already cached.

## Fetch button semantics

The Fetch button is the only path that talks to the network for
layer data. Its job per cycle:

```
plan = step_options for current cycle
need_sfc_steps = [s for s in plan if not is_grib_cached(cycle, s, "sfc")]
need_pl_steps  = [s for s in plan if not is_grib_cached(cycle, s, "pl")]

download(sfc) for each step in need_sfc_steps
download(pl)  for each step in need_pl_steps
```

Whether to download `pl` at all is a session-level toggle (default
yes, can be turned off in the fetch confirm dialog if the user only
cares about surface fields and wants the cycle to fetch in half the
time). `sfc` is always on because all currently-implemented surface
layers fit in one round-trip per step.

`更新 / Update` button is the same flow against a newer cycle.

## What changes vs. the current code

| File | Today | Target |
|---|---|---|
| `services/forecast_service.py::ForecastRequest` | `(run_time, step_hours, param, level)` | `(run_time, step_hours, kind)` |
| `services/forecast_service.py::ForecastService._download` | one param + optional level per call | full param-list (and levelist for pl) per kind |
| `services/forecast_service.py::grib_cache_path` | filename includes `<param>@<level>` | filename is `<step>h-<kind>.grib2` |
| `services/forecast_service.py::is_grib_cached` | takes `param, level` | takes `kind` |
| `products/catalog.py::DataField` | has `level` and `ecmwf_param` | unchanged values; adds a `kind` property derived from `level` |
| `figures/_scalar_chart.py` extractors | assume single-level / single-param GRIB | filter by level with `.sel(isobaricInhPa=L)` so they work on the multi-band GRIB |
| `figures/msl_chart.py` / `t2m_chart.py` / `tp_chart.py` / `wind_chart.py` | read by variable name; works as-is on multi-band sfc GRIBs | unchanged once the service writes multi-band sfc GRIBs |
| `figures/render_pool.py::render_layer` | dispatches on `layer_key` | unchanged; only the *caller's* choice of `grib_path` changes |
| `components/map_view.py` ForecastRequest call sites | pass `param=selected_field.ecmwf_param, level=selected_field.level` | pass `kind=selected_field.kind` |
| `components/map_view.py::_download_loop` | iterates one param per step | iterates kinds per step; for each, calls download once |
| `components/map_view.py::_ensure_rendered` | builds path with `param=field.ecmwf_param, level=field.level` | builds path with `kind=field.kind` |
| `components/map_view.py` Fetch button + confirm dialog | tally per-layer cache | tally per-kind cache; show "X surface frames + Y pressure-level frames to download" |

## Open questions to confirm with the user

1. **Pressure-level fetch by default?** The pl GRIB is much bigger than
   sfc (≈ 5 params × 7 levels = 35 fields per step vs ~9 for sfc). If
   the user mostly looks at surface charts, pre-fetching pl every
   cycle wastes bandwidth. Default ON or OFF? Suggested: ON by
   default, with a checkbox in the fetch confirm dialog to skip pl.

2. **Wind direction arrows.** The user agreed wind at pressure levels
   ships as speed-only. Surface wind10m still draws arrows. Should the
   matrix UI render a separate "風向" row alongside the "風速" wind
   chips, or stay speed-only everywhere? Suggested: keep speed-only
   for v1; revisit when we have a vector renderer that scales.

3. **MSL overlay across kinds.** The current MSL contour overlay uses
   the sfc msl GRIB. Pressure-level layers asking for it now read from
   the same sfc file; no separate fetch path needed. Confirm the
   overlay stays at MSL (not, say, gh@500 over t@850).

4. **Frame memory budget.** With 8 surface layers + 19 pressure-level
   layers = 27 layers × 9 regions × 65 steps = 15 795 potential PNGs
   in `frames`. At ~50 KB/PNG that's ~750 MB. `FRAMES_CACHE_LIMIT`
   is currently 500. Probably want a per-cycle bound and to evict
   the previous cycle wholesale rather than FIFO across cycles.

## Migration order (when ready to implement)

1. **catalog**: add `DataField.kind` property; no other change.
2. **forecast_service**: change `ForecastRequest`, `_download`,
   cache path, `is_grib_cached`, `grib_cache_path` to use `kind`.
3. **extractors** in `_scalar_chart.py`: add `.sel(isobaricInhPa=L)`
   when the config has a level.
4. **map_view**: change every `ForecastRequest(...)` /
   `grib_cache_path(...)` / `is_grib_cached(...)` call site to pass
   `kind=` instead of `param=`/`level=`.
5. **map_view._download_loop**: iterate `kinds` per step instead of
   one param per step.
6. **fetch_confirm dialog**: tally per kind, optional pl-skip
   checkbox.
7. Delete dead per-param caches under the user's data dir on first
   run with the new layout (or just leave them — they're inert and
   the new cache lives in a different filename pattern).
