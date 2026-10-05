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
| JMA daily records (redistributed) | 1880 → yesterday | Japan, 1,342 stations incl. closed |
| JMA 府県天気予報 | Forecast (today → +7 days) | Forecast office area |
| Open-Meteo | Forecast (point) | Single lat/lon |

Forecast maps and grids come from ECMWF. JMA supplies observed conditions
(radar, AMeDAS) and the 府県天気予報 card on the point forecast view
(`services/jma_forecast_service.py`, cached 1 hour, keyed by forecast office).

## JMA endpoints are not an official API

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
The strings are defined in `services/jma_endpoints.py` (`ATTRIBUTION`,
`ATTRIBUTION_PROCESSED`). `figures/footer.py` has no JMA branch yet; add
one there when a JMA figure is rendered through it.

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
- Cache tiles by (basetime, validtime, z, x, y) under
  `<data_dir>/jma/radar/` (see "Caching" below)
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

Implemented as the 今 panel of the 観測 tab (`components/amedas_view.py`,
figure `figures/amedas_map.py`). Temperature and wind reuse the model
charts' anchors (`T2M`, `WIND10M`) so a colour means the same value across
the app; precipitation uses JMA's bins. The land / coastline under the stations
is Natural Earth 10m clipped to the map frame
(`figures/_japan_coastline.npz`, built once by
`figures/_precompute_japan_coast.py`); the 0.25° masks the model charts use
are too coarse for station spacing. cartopy runs only in that precompute.

## Daily records since 1880 (redistributed)

JMA publishes daily values one station-month at a time as HTML. The
個人開発気象統計 site (`WeatherStatic/`) collects them and redistributes them
as NetCDF under `/data/daily/` (one file per year with all stations, one per
station with its whole record, and `manifest.json` with sha256 per year).
`services/observation_archive.py` reads these; `observation_stats.py` holds
the arithmetic; the 観測 tab's day / period / station panels use them. URLs
live in `services/observation_endpoints.py`. Like JMA itself this is not a
`config.toml` source.

Statistics follow JMA practice and the site's rules: 資料不足 values
(quality ≤ 4) are shown marked in a single day's list but never enter
counts, extremes, means or totals; ties share a rank and are ordered north
to south; the latest day is reported for a repeated extreme.

## Caching

`<data_dir>` is the `data_dir` setting in `config.toml`; when unset it is
`~/.cache/aiseed-weather` (`resolved_data_dir()` in `models/user_settings.py`).

- Radar: `<data_dir>/jma/radar/_latest_meta.json` for freshness; tiles are
  planned at `<data_dir>/jma/radar/<basetime>/<validtime>/<z>/<x>/<y>.png`
  (`jma_radar_service.py` is still a skeleton)
- AMeDAS latest snapshot: `<data_dir>/jma/amedas/_latest_snapshot.json`
  (10-minute window)
- AMeDAS station metadata: `<data_dir>/jma/amedas/amedastable.json`
  (refresh weekly; station list is essentially stable)

## Forbidden

- Background polling of any JMA endpoint
- Concurrent fetches faster than 4 per second (be conservative)
- Hardcoding tile URLs without fetching the index first
- Reusing JMA data older than its update cadence and presenting it as "current"
- Omitting the attribution
- Treating JMA endpoints as a documented API contract — they are not
- Adding a JMA key to `config.toml` (JMA use is per-feature)
- Importing `flet` in `services/`
