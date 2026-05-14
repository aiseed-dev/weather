---
name: weather-rendering
description: How to render synoptic-quality weather maps and embed them in Flet. Read when modifying components or modules that produce figures.
---

## Audience reminder

The user reads synoptic charts. Render in conventions they recognize from
JMA, NOAA, ECMWF, DWD, and Met Office products — not "weather app" prettified
versions. When in doubt, match the visual language of professional synoptic
charts.

## Stack

- `matplotlib` for figure creation
- `cartopy` for map projections and coastlines
- `flet.matplotlib_chart.MatplotlibChart` to embed figures in Flet
- All figure-building code lives in `figures/` as pure functions: take
  data, return a `matplotlib.figure.Figure`. No Flet imports there.

## Projections

| View | Projection | Notes |
|------|-----------|-------|
| Global synoptic | `Robinson` or `PlateCarree` | Robinson preferred for sharing; PlateCarree for analysis with lat/lon grid |
| Northern Hemisphere | `NorthPolarStereo` | Standard for jet stream and polar vortex |
| Mid-latitude band | `Mercator` | Familiar weather map look |
| Japan / regional | `PlateCarree` with `set_extent` | Or `LambertConformal` for higher latitudes |
| Tropical | `PlateCarree` | Standard for typhoon tracking |

Choose projection in the figure-building function based on the view request.
Never hardcode projection in services.

## Standard layers (synoptic conventions)

### Mean sea level pressure (`msl`)
- Contour lines (isobars) at **4 hPa intervals**, labeled
- Bold every 20 hPa
- 1016 hPa line slightly emphasized (mean atmospheric pressure)
- Convert Pa → hPa before plotting
- Mark L (low) and H (high) centers — extrema within a regional window

### 2m temperature (`2t`)
- Filled contours (`contourf`) with diverging colormap (`RdBu_r`)
- 0°C line emphasized
- Convert K → °C before plotting
- Contour interval: 4°C in mid-latitudes, 2°C for regional

### 10m wind (`10u`, `10v`)
- Barbs (preferred for synoptic charts) — `ax.barbs(...)` — at thinned grid
- Or streamplot for visual flow
- Wind speed shading optional underneath
- Barb interval: thin grid to ~50 barbs across the view

### Geopotential at 500 hPa (`gh` at 500)
- Contour lines at **60 gpm intervals** (5640, 5700, 5760, …) — the synoptic standard
- Convert gpm to dam (decimeters) for labels: 564, 570, 576
- Often paired with temperature or anomaly shading below

### Precipitation (`tp`)
- Filled contours, sequential colormap
  - Light rain: `Blues` (0–10 mm)
  - Heavy rain: extended palette to include purple/red for >50 mm
- Use accumulation intervals (3h, 6h, 24h) — never show "instantaneous"
- Convert m → mm

### Jet stream (`u`, `v` at 250 hPa)
- Wind speed shading, contour interval 10 m/s starting at 30 m/s
- Use a perceptually uniform colormap (`magma_r` or `viridis`)
- Optional streamlines overlay

## Anomaly layers

See `climatology-analysis` skill. Key rendering rules:

- Always diverging colormap centered at zero
- Symmetric vmin/vmax
- Reference period in the title or footer
- Lock the color range across timesteps in animations

## Standard rendering pattern

```python
# figures/msl_chart.py
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
from .footer import apply_footer  # see figure-export skill

def render_msl(ds, *, projection="robinson", run_id: str) -> plt.Figure:
    fig = plt.figure(figsize=(12, 7))
    proj = _projection(projection)
    ax = plt.axes(projection=proj)
    ax.coastlines(linewidth=0.6)
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#888", alpha=0.5)

    msl_hpa = ds["msl"] / 100.0  # Pa to hPa
    cs = ax.contour(
        ds.longitude, ds.latitude, msl_hpa,
        levels=range(940, 1060, 4),
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=0.7,
    )
    ax.clabel(cs, inline=True, fontsize=7, fmt="%d")

    valid_time = ds["valid_time"].values
    fig.suptitle(f"MSL [hPa] — valid {valid_time}", fontsize=13)
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)
    return fig
```

## Performance rules

- Rendering happens in `asyncio.to_thread` (matplotlib is synchronous)
- Show a `ft.ProgressRing` placeholder during render
- Cache rendered PNGs keyed by `(run_time, layer, projection, anomaly_ref)`
- For animation: pre-render all timesteps via `asyncio.gather` with a
  thread pool semaphore (max 4 concurrent renders)

## Figure cleanup

Always close figures after use:

```python
plt.close(fig)
```

Animation sessions otherwise leak memory.

## DPI separation

Screen rendering uses lower DPI for speed. Export re-renders at the chosen
DPI. Do not reuse the screen figure for export — re-call the figure-builder.

See `figure-export` skill for the export pipeline.

## Forbidden

- Calling `plt.show()` (blocks the event loop)
- Using `pyplot.gcf()` / global state (always create new `Figure` objects)
- Hardcoded color scales scattered in code — define in `figures/colormaps.py`
- "Prettifying" charts away from synoptic conventions (rainbow MSL contours,
  emoji weather icons, etc.) — these are anti-features for this audience
- Embedding interactive matplotlib widgets (use Flet controls instead)
- Skipping the footer / attribution
