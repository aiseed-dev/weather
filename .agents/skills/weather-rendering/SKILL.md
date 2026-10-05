---
name: weather-rendering
description: Meteorological conventions for synoptic-quality weather maps and how rendered charts reach the Flet UI. Read when modifying components or modules that produce figures.
---

## Audience reminder

The user reads synoptic charts. Render in conventions they recognize from
JMA, NOAA, ECMWF, DWD, and Met Office products — not "weather app" prettified
versions. When in doubt, match the visual language of professional synoptic
charts.

## Stack

- Map charts (ECMWF grids) are rendered with numpy + contourpy + PIL.
  `figures/_layered_renderer.py` composites each variable from its
  `ChartSpec` in `figures/_chart_specs.py`. The layer structure,
  palettes, and isoline intervals are defined in the `chart-base-design`
  skill.
- cartopy runs only offline, to precompute coastline and land masks
  (`figures/_precompute_coastlines.py` → `_coastline_masks.npz`). It is
  not on the render path.
- Map renderers return PNG bytes, and the view shows them with `ft.Image`.
- The point forecast chart is drawn on `flet.canvas`
  (`figures/canvas_timeseries.py`). matplotlib is used for its PNG
  download (`figures/point_forecast_chart.py`).
- Figure-building code lives in `figures/` as functions that take data
  and return an image. Apart from `canvas_timeseries.py`, which builds
  canvas shapes, modules in `figures/` do not import Flet.

## Projections

Regions and their projections are defined in `figures/regions.py`:

| Region | Projection |
|--------|-----------|
| Global and regional presets (Japan, East Asia, N. Pacific, …) | PlateCarree |
| Arctic / Antarctic | Polar equidistant-azimuthal (precomputed reindex table) |
| User-defined custom region | Mercator |

The figure side chooses the projection from the region. Services do not
hold projection settings.

## Standard layers (synoptic conventions)

Palettes, isoline intervals, and which variables get isolines are
defined once, in `chart-base-design` and `figures/_chart_specs.py`.
This section lists the meteorological conventions that sit on top of
them.

- **`msl`**: convert Pa → hPa. Isobars are the primary value carrier.
  Mark L (low) and H (high) centers as extrema within a regional window.
- **`2t`**: convert K → °C. Diverging palette anchored at 0 °C,
  colour-only.
- **`10u`, `10v`**: wind speed shading with thinned direction glyphs
  (`figures/wind_chart.py`).
- **`gh` at 500 hPa**: isohypses at 60 gpm (5640, 5700, 5760, …), the
  synoptic standard.
- **`tp`, `tprate`**: convert to mm (`tp`) or mm/h (`tprate`). State the
  accumulation interval or rate in the label.
- **Jet stream (`u`, `v` at 250 hPa)**: wind speed shading starting at
  30 m/s, optional streamlines.

## Anomaly layers

See `climatology-analysis` skill. Key rendering rules:

- Always diverging colormap centered at zero
- Symmetric vmin/vmax
- Reference period in the title or footer
- Lock the color range across timesteps in animations

## Reference implementation

`figures/msl_chart.py` is a thin wrapper that calls
`_layered_renderer.render(MSL, …)`. New map variables add a `ChartSpec`
to `_chart_specs.py` the same way. The layered renderer writes source,
license, run id, and layer into PNG text metadata (`_png_metadata`). It draws no
visible footer yet; `figures/footer.py` (`apply_footer`) works on
matplotlib figures only.

## Performance rules

- UI code renders through `render_layer_async` in
  `figures/render_pool.py`, which runs the render in `asyncio.to_thread`.
- Show a `ft.ProgressRing` placeholder during render.
- `MapView` caches rendered PNG frames keyed by
  `(cycle, region, layer, overlays, step)`.
- Animation frames are pre-rendered by one background thread, one frame
  at a time. Results reach component state through an `asyncio.Queue`
  drained by a task started with `page.run_task`.

## matplotlib figures

Where matplotlib is used (the point forecast PNG), create a new
`Figure` per render and close it with `plt.close(fig)` afterwards.
Repeated renders otherwise leak memory.

## DPI separation

Screen rendering uses lower DPI for speed. Export re-renders at the chosen
DPI. Do not reuse the screen figure for export — re-call the figure-builder.

See `figure-export` skill for the export pipeline.

## Forbidden

- Calling `plt.show()` (blocks the event loop)
- Using `pyplot.gcf()` / global state (always create new `Figure` objects)
- Hardcoded color scales scattered in code — define them as `ChartSpec` anchors in `figures/_chart_specs.py`
- "Prettifying" charts away from synoptic conventions (rainbow MSL contours,
  emoji weather icons, etc.) — these are anti-features for this audience
- Embedding interactive matplotlib widgets (use Flet controls instead)
- Skipping the footer / attribution
