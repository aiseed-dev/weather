---
name: chart-base-design
description: Structural design of synoptic chart layers — base map, data overlay, isolines, pill labels. Read when implementing or modifying any chart renderer in figures/. Pairs with weather-rendering (which covers meteorological conventions); this skill covers the visual layering and palette structure for the numpy+PIL fast path.
---

## Status: starting point, expected to evolve

This skill records the design principles derived from user feedback in
the conversation on 2026-05-15. **The concrete values below — gray
shades, alpha, isobar widths, palette ranges, pill sizing — are
CALIBRATION POINTS, not specifications.** As we add variables and
learn what reads well, expect to revisit them.

What should change much less often: the **structure** (layered
approach, luminance hierarchy, continuous LUT, white-isobar
convention, pill labels on lines).

## What this skill is not

A consumer weather app design guide. Avoid commercial-app conventions
that previous iterations of this repo accidentally absorbed:

- No pastel-coloured isobars (yellow, pink, etc.) — they were the
  "Windy-style" notion that was wrong; Windy's actual isobars are
  white.
- No near-white coastlines that vanish on white-zero palettes.
- No per-variable hand-tuned palette stops driven by "what looks good".
- No fps-budget framing — the analyst opens a chart and studies it
  for minutes, not 16 ms per frame.
- No matplotlib + cartopy at runtime — we use numpy + PIL + contourpy
  exclusively. cartopy is precompute-only (coastline / land masks).

The reference visual is Windy's MSL chart: flat gray base, transparent
diverging data overlay, white isobars at the WMO synoptic interval,
inline pill labels, dark coastlines. Match the **layering**; the
colour choices below are first cuts.

## Layer order (bottom to top)

The renderer composites in this order:

1. **Base map** — flat gray, two shades for land/sea (sea slightly
   cooler, land slightly warmer, same luminance band). From
   `_basemap.base_map_rgb(region_key)`. The base map is the SAME
   across every variable in a given region. **Coastlines are NOT
   baked into this layer.**
2. **Data overlay** — partially transparent colour from the variable's
   continuous LUT, alpha-blended over the base. Where the variable
   is "below threshold" or has no meaningful value (e.g. precipitation
   below 0.1 mm), alpha = 0 so the base shows through unchanged.
3. **Isolines** — single colour (white) thin lines at the variable's
   convention interval, drawn on a 2× supersampled copy of the
   composite. **The colour is the same across all levels; the line
   position carries information, not its hue.**
4. **Pill labels** — rounded-rectangle value markers placed ON the
   isolines (not next to them), with the pill background coloured by
   the same data palette at that isoline's value. White text on top.
   Drawn at supersample with a 2× font so they Lanczos-downsample
   to the intended visual size.
5. **Coastlines** — thin near-black 1 px line, stamped on the
   **native-resolution downsampled output**. Stamping before the
   supersample round-trip puts the line through a LANCZOS filter
   that washes it out from #050508 to a muddy mid-tone; stamping
   after keeps it crisp at full luminance. The coastline is the
   one layer that genuinely wants aliased 1 px hard edges, not
   antialiased ones.

## Luminance hierarchy

The chart reads because the layers occupy distinct luminance bands
and don't compete:

| Layer       | Luminance        | Carries                          |
|-------------|------------------|----------------------------------|
| Coastline   | very dark        | geographic boundary              |
| Land / Sea  | mid-low gray     | land/sea differentiation, base   |
| Data        | mid (with alpha) | value as supporting hint         |
| Isoline     | white            | value as primary information     |
| Pill label  | matches data     | local value anchor on the line   |

Adding a layer outside this hierarchy (coloured isobars, saturated
opaque data fills, multi-coloured contour lines) breaks the
readability that makes the chart work.

## Current calibration values

```
SEA_RGB             = #585c64
LAND_RGB            = #767a80
COASTLINE_RGB       = #050508  (near-pure black; the line is stamped
                                AFTER the supersample/downsample
                                round-trip to stay crisp)
ISOLINE_RGB         = #ffffff
ISOLINE_WIDTH_THIN  = 1 px (at supersample resolution)
ISOLINE_WIDTH_BOLD  = 2 px (at supersample resolution)
ISOLINE_SUPERSAMPLE = 2  (render isolines at 2× then LANCZOS downsample
                          → effective ~0.5 px antialiased line)
SMOOTH_SIGMA        = 3.0  (gaussian pre-smoothing of the field in grid
                            cells, before contouring — suppresses
                            small-scale noise without rounding off
                            synoptic features)
MIN_SEGMENT_VERTICES = 30  (drop short polyline fragments that don't
                            carry information at this scale)
DATA_ALPHA          = 0.45
PILL_TEXT_RGB       = #ffffff
PILL_RADIUS         = text_height / 2
PILL_PAD_X          = 4 px
```

## Isoline rendering — supersample + downsample

PIL's `ImageDraw.line` has a 1 px integer minimum stroke width.
Native 1 px white lines on this chart size still read chunky.
Rendering at 2× resolution with width=1 and downsampling with
LANCZOS yields an antialiased ~0.5 px effective line without
adding a dependency for a vector toolkit. The composite (base +
data + coastline) is upsampled with NEAREST first so the gray
land/sea and the dark coastline stay crisp through the round-
trip — only the new line geometry benefits from the downsample
filter.

## Palette differentiation by data source (NWP vs observation)

A subtle but operationally critical rule: **two layers must not share
a palette when their epistemic precision is fundamentally different.**

Specifically, NWP-derived precipitation (ECMWF Open Data ``tp`` and
``tprate``) and observed precipitation (JMA radar) must use distinct
colour schemes — otherwise an analyst reading "yellow = 20 mm/h"
treats both as equally authoritative, when:

- A **radar** pixel at 20 mm/h is a measurement at 1 km × 5 min
  resolution. The intensity is real, the location is real.
- An **NWP tprate** pixel at 20 mm/h is the output of a parameterised
  convection scheme on a 28 km grid. The value is a regional
  area-mean of an under-resolved process. Sub-grid heavy-rain
  phenomena (線状降水帯 50-200 km × 20-50 km, ゲリラ豪雨 5-20 km,
  typhoon eyewall peaks) are systematically under-represented or
  smeared.

Same colour, very different epistemic weight. The chart must not
flatten that difference.

### Current assignment

| Data source             | Palette family             | Notes |
|-------------------------|----------------------------|-------|
| ECMWF tp, tprate (NWP)  | Windy 3-hour palette       | Windy calibrated this scale for NWP precipitation — its bins (1.5 / 2 / 3 / 7 / 10 / 20 / 30) match the precision NWP can actually deliver, and don't claim local accuracy at extreme intensities. |
| JMA radar (future)      | **Reserved — distinct palette TBD** | When the radar layer lands, pick a different hue family (e.g., cool blues with red high-end, vs Windy's pale-cyan-to-magenta) and finer bins matching the radar's 1 km / 5 min precision. |

### Principle

> Visual resolution should match data resolution. If your colour
> bins are finer than your data is accurate, the chart lies. If
> they're coarser, the chart wastes information. Match the bin
> cadence (and the palette identity) to the source.

This is why the Windy palette is "honest" for NWP — its bins stop
at 30 mm/h rather than running up to 200 mm/h that NWP can't
reliably distinguish, and it doesn't compete with a future radar
palette for the analyst's visual category of "real precipitation".

## Which variables get isolines? (physics-driven, not aesthetics)

Two readability modes split the catalogue:

### Colour-only (no isolines drawn)

Variables whose field at the rendered resolution is **not smooth
enough for clean iso-lines to be physically meaningful**. Drawing
isolines on these fields produces dense tangles at the
boundary-layer / land-sea / orography discontinuities and only
clutters the chart.

Examples:

- **2 m temperature (t2m)** — diurnal heating, land-sea contrast,
  orography, urban heat island all produce strong sub-synoptic
  gradients. A summer-afternoon Japan chart at 2 m has ~10 °C of
  contrast inside a few grid cells along every coastline.
  Plotting 2 °C isotherms would draw a wall of parallel lines
  around each shore; gaussian smoothing strong enough to suppress
  them would also wipe out the synoptic temperature pattern.
- **Precipitation totals**, **snow depth**, **cloud cover** — same
  reason at the boundary layer / surface.

Render these with the data overlay alone. The continuous LUT and
the legend bar carry the value information.

### Colour + isolines

Variables whose field is **synoptic-scale smooth and where line
shape carries the analysis information**: troughs, ridges, jets,
fronts. Iso-lines here are not a decoration but the primary value
carrier; the colour fill is supporting.

Examples:

- **Mean sea level pressure (msl)** — 2 hPa isobars trace the
  synoptic pressure pattern. The surface layer's noise on pressure
  is small relative to the 2 hPa contour interval after a σ=3
  gaussian.
- **Geopotential height at pressure levels (gh500, gh850, …)** —
  every 60 gpm at 500 hPa is the synoptic convention.
- **Upper-air temperature** (`t` at 850, 500, 250 hPa) — above the
  boundary layer the temperature field smooths out and an 850-hPa
  isotherm has real synoptic meaning (frontal positions, thermal
  troughs).
- **Wind speed at upper levels** — jet-axis lines.

### How to decide for a new variable

Ask: *can this field be drawn as clean iso-lines at the chosen
contour interval after a synoptic-scale smoothing pass?* If yes,
it's "colour + isolines". If the smoothing erases the synoptic
pattern before it erases the noise, it's "colour-only".

This is a **physics test**, not an aesthetic preference. The first
draft of this skill called the split "what carries the value
better" — but the deeper reason is whether the field itself is
isoline-tractable at the rendered resolution.

## Palette construction — continuous LUT, not binned

Each variable carries `(vmin, vmax, palette_family)` rather than
hand-tuned bin edges. The renderer:

```python
norm = np.clip((data - vmin) / (vmax - vmin), 0.0, 1.0)
idx  = (norm * 255.0).astype(np.uint8)
rgb  = lut_256[idx]            # (256, 3) uint8
```

Why continuous, not `np.digitize`:

- **Faster**: 17 ms vs 45 ms on a 721×1440 grid (measured 2026-05-15).
- **No posterisation**: data reads as the continuous field it is.
- **Less metadata to hand-pick**: no bin edge guessing per variable.
- **One LUT per palette family**, shared across variables that use it.

## Palette families

Five families cover every variable we ship:

| Family              | When to use                                          |
|---------------------|------------------------------------------------------|
| `diverging`         | Natural-zero or reference-value variables: MSL       |
|                     | (anchor at 1013 hPa), 2m T (0 °C), geopotential      |
|                     | anomaly, vorticity                                   |
| `sequential_cool`   | Positive-only "cool" variables: precipitation, snow  |
|                     | depth, cloud cover, total column water               |
| `sequential_warm`   | Positive-only "warm" variables: CAPE, wind speed,    |
|                     | shortwave radiation                                  |
| `cyclic`            | Directional variables on 0..360°: mean wave dir,     |
|                     | wind direction                                       |
| `categorical`       | Discrete codes: precipitation type                   |

If a new variable doesn't fit a family, the family list is incomplete;
do not add per-variable palettes.

## Isoline spacing

WMO / ECMWF synoptic conventions, **not** JMA's pen-era surface chart
(4 hPa) which is preserved as historical continuity rather than as
the analysis default.

| Variable          | Thin interval | Bold every |
|-------------------|---------------|------------|
| MSL               | 2 hPa         | 20 hPa     |
| 500 hPa geopot.   | 60 gpm        | 300 gpm    |
| 850 hPa temp      | 3 °C          | 15 °C      |

Earlier sessions widened MSL to 8 hPa to "let the chart breathe" —
that was treating the symptom (pale-yellow lines blurring on an
opaque posterised fill) instead of the cause. With white lines on a
transparent overlay, 2 hPa reads cleanly even on a regional crop.

## Pill labels

- Drawn at intervals along each isoline, **on** the line (rounded
  rectangle behind the text, line passing through the pill centre).
- Background = data palette colour evaluated at the isoline's value
  (so a 1024 hPa pill has the same shade as the 1024 hPa data area
  the line bounds).
- Text = white, centred.
- Spacing along the isoline: tuned by region size — denser pills on
  a wide regional chart, sparser on a tight zoom.
- Pills replace inline `clabel`-style numbers, NOT the chart legend
  bar. Both exist together (legend bar = continuous scale reference,
  pill = local value anchor).

## Special-case: JMA pen-era surface chart mode

A separate render path can reproduce JMA's hand-drawn surface chart
(pale-green land, pale-blue sea, **black** isobars at 4 hPa, no data
fill, fronts as red/blue lines, L/H labels in Japanese). This is a
historical-continuity export option, NOT the default. When the time
comes to implement it, gate it behind an explicit user choice; do
not let it leak back as the analysis path.

## File map

| File                              | Role                                  |
|-----------------------------------|---------------------------------------|
| `figures/_basemap.py`             | `base_map_rgb(region_key)` — the layer-1 RGB |
| `figures/_coastlines.py`          | Coastline mask stamp (the dark line on top of data) |
| `figures/_coastline_masks.npz`    | Precomputed land + coastline masks per region |
| `figures/_precompute_coastlines.py` | One-time generator (run after region or NE update) |
| `figures/msl_chart.py`            | Reference implementation of all four layers |
| `figures/_fast.py`                | Shared crop / polar reindex / palette helpers |
| `figures/_scalar_chart.py`        | Generic palette-driven renderer (being migrated) |

## When this skill changes

Update this file (don't just amend code) when:

- The luminance hierarchy or layer order changes (rare, structural).
- A new palette family is added (rare, every five families covers a
  large catalogue).
- A new isoline-bearing variable picks an interval (occasional).
- The pill-label algorithm gains a real placement model (when we move
  past "midpoint of the longest segment").

For pure colour-tweaks within a family, change the LUT generator
inline; don't touch this skill.
