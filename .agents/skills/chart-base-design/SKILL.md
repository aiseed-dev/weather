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
   baked into this layer** — they go on top (see step 3) so the
   alpha-blend doesn't dilute them.
2. **Data overlay** — partially transparent colour from the variable's
   continuous LUT, alpha-blended over the base. Where the variable
   is "below threshold" or has no meaningful value (e.g. precipitation
   below 0.1 mm), alpha = 0 so the base shows through unchanged.
3. **Coastlines** — thin near-black 1 px line, stamped on top of the
   alpha-blended composite. Must be drawn after the blend, otherwise
   a 0.45 alpha turns a #18181c coastline into a #807a78 mid-tone
   that reads almost-white on a light-beige data area.
4. **Isolines** — single colour (white) thin lines at the variable's
   convention interval. **The colour is the same across all levels;
   the line position carries information, not its hue.**
5. **Pill labels** — rounded-rectangle value markers placed ON the
   isolines (not next to them), with the pill background coloured by
   the same data palette at that isoline's value. White text on top.

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
COASTLINE_RGB       = #18181c
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
