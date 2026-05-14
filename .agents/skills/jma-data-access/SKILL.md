---
name: jma-data-access
description: How to fetch JMA (Japan Meteorological Agency) nowcast data — rainfall radar and AMeDAS surface observations. Read when modifying services or components that show current Japanese weather conditions.
---

## Position in this project

JMA provides the **"Japan, right now"** layer. It complements but does not
overlap the other data sources:

| Source | Time scale | Spatial scale |
|--------|-----------|---------------|
| ECMWF Open Data | Forecast (now → +10 days) | Global grid |
| ERA5 | Historical (1940 → ~5 days ago) | Global grid |
| **JMA radar / AMeDAS** | **Nowcast (now, last ~6 h)** | **Japan only** |
| Open-Meteo | Forecast (point) | Single lat/lon |

JMA is **not** used as a forecast source in this app. Predicted weather comes
from ECMWF; current observed conditions come from JMA.

## Important: JMA endpoints are not an official API

The JSON and image endpoints on `www.jma.go.jp` and `tile.jmaxml.jp` are not a
documented public API. JMA uses them internally on their website and has
stated they can change without notice. Treat them as best-effort.

Implications:
- **Wrap every request in try/except.** Surface failures to the user clearly,
  do not retry aggressively.
- **Pin a service contract per endpoint** in this module. If JMA changes the
  schema, the failure surface is one file, not the whole app.
- Centralize all JMA URLs in `services/jma_endpoints.py` so a single change
  fixes the project.
- Verify endpoint URLs at the start of any JMA-related task; the layout
  documented here may have shifted since this skill was written.

## Attribution (mandatory)

Every figure or display that uses JMA data must show:

> 出典: 気象庁ホームページ (https://www.jma.go.jp/)

If the data has been processed or composited (e.g. radar overlaid on a map):

> 出典: 気象庁ホームページ
> 編集・加工を行った旨と編集責任が利用者にあります

This text appears in the figure footer and in embedded metadata.
Implement in `figures/footer.py` with a JMA-specific branch.

## Etiquette

JMA's servers are public-facing and not built for high-volume API use. Be a
good citizen:

- **No background polling.** Fetches happen only when the user opens the
  relevant view (see `user-action-fetch` skill).
- **Cache for the full update cadence** of each dataset (radar 10 min, AMeDAS
  10 min). Within that window, never refetch.
- **No parallel pre-fetch** of regions the user has not asked for.
- **User-Agent**: identify the app honestly:
  `"AIseed Weather/0.1 (+https://aiseed.dev)"`

## Rainfall radar (高解像度降水ナウキャスト)

JMA publishes radar composites as map tiles plus index files.

### Update cadence
- New observation every **5 minutes** (10 minutes for the "high resolution"
  product depending on layer); the latest run is published with a small lag
- This app refreshes at most every **10 minutes** to stay comfortably below
  publisher cadence

### Endpoints (verify before implementing)
- Time index: `https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json`
  - Returns the list of available basetimes and their valid times
- Tiles: `https://www.jma.go.jp/bosai/jmatile/data/nowc/<basetime>/none/<validtime>/surf/hrpns/<z>/<x>/<y>.png`
  - Standard XYZ tile scheme, z (zoom), x, y
  - Transparent PNG; render on top of a base map

These paths have changed in past JMA updates. **Always fetch the
targetTimes index first** and extract the URL pattern from there, rather
than hardcoding deeply nested paths.

### Rendering approach
- Use a base map of Japan (cartopy `LambertConformal` centered on Japan, or
  `PlateCarree` with `extent=[122, 146, 24, 46]`)
- Overlay the radar tiles at appropriate zoom
- Apply the standard JMA precipitation colormap (0-1, 1-5, 5-10, 10-20,
  20-30, 30-50, 50-80, 80+ mm/h) — use JMA's own color values for
  readability familiarity
- Do not invent a new color scale; users recognize the JMA scheme on sight

### Tile fetching strategy
- For one viewport, fetch only the tiles that intersect the visible extent
- Cache tiles by (basetime, validtime, z, x, y) in
  `~/.cache/aiseed-weather/jma/radar/`
- Tiles for old basetimes can be deleted aggressively (>2 hours old)

## AMeDAS (地上気象観測)

AMeDAS provides ground observations from ~1,300 automated stations across
Japan: temperature, precipitation, wind, sunshine, snow depth (where applicable).

### Update cadence
- Updated every **10 minutes** (some products 1 hour)
- Refresh the in-app view at most every 10 minutes

### Endpoints (verify before implementing)
- Station metadata: `https://www.jma.go.jp/bosai/amedas/const/amedastable.json`
  - Maps station ID → name, lat/lon, type
- Latest observation index: `https://www.jma.go.jp/bosai/amedas/data/latest_time.txt`
- Map snapshot: `https://www.jma.go.jp/bosai/amedas/data/map/<YYYYMMDDHHMMSS>.json`
  - JSON of all stations' latest values
- Per-station time series:
  `https://www.jma.go.jp/bosai/amedas/data/point/<station_id>/<YYYYMMDD>_<hh>.json`

### Data structure
The map JSON keys are station IDs (strings, zero-padded). Each value is an
object with sub-arrays per variable, where each sub-array is `[value, quality_flag]`.
Variables present depend on station type:

- All stations: temperature, precipitation
- Wind-equipped: wind direction, wind speed
- Sunshine-equipped: sunshine duration
- Snow-equipped: snow depth

Always check for variable presence before reading; not all stations have all variables.

### Rendering approach
- Plot stations as markers on a Japan basemap
- Color-code or size-code by the selected variable
- Show station name and value on hover/tap
- For temperature: diverging colormap centered at a context-appropriate value
- For precipitation: use the JMA precipitation color scale at hourly intervals
- Always show the snapshot time prominently — AMeDAS is "as of HH:MM"

## Caching

- Radar tiles: `~/.cache/aiseed-weather/jma/radar/<basetime>/<validtime>/<z>/<x>/<y>.png`
- AMeDAS map JSON: `~/.cache/aiseed-weather/jma/amedas/map_<YYYYMMDDHHMMSS>.json`
- AMeDAS station metadata: `~/.cache/aiseed-weather/jma/amedas/amedastable.json`
  (refresh weekly; station list is essentially stable)

## Forbidden

- Background polling of any JMA endpoint
- Concurrent fetches faster than 4 per second (be conservative)
- Hardcoding tile URLs without fetching the index first
- Reusing JMA data older than its update cadence and presenting it as "current"
- Omitting the attribution
- Treating JMA endpoints as a documented API contract — they are not
- Including JMA in the first-run setup selection (it is a per-feature flow)
- Importing `flet` in this directory
