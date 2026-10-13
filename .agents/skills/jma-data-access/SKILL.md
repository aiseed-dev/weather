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
- New observation every **5 minutes**, published a few minutes late
- The time index is reused for **10 minutes** (再取得 bypasses it)

### Endpoints (checked 2026-10-13)
- Time index, observations: `.../jmatile/data/nowc/targetTimes_N1.json` —
  the past 3 hours, `basetime == validtime`
- Time index, forecast: `.../jmatile/data/nowc/targetTimes_N2.json` —
  +5 … +60 min from the latest basetime
- Each entry lists its `elements`; the precipitation layer is `hrpns`
- Tiles: `.../jmatile/data/nowc/<basetime>/none/<validtime>/surf/hrpns/<z>/<x>/<y>.png`
  - 256 px Web-Mercator XYZ tiles, times in UTC (`YYYYmmddHHMMSS`)
  - **Only even z carry data**; an odd z, or z ≥ 12, returns a blank tile
  - Palette PNGs in JMA's eight legend colours, transparent where no echo

All URLs are in `services/jma_endpoints.py`. Pick frames from the index;
never build a time that the index did not list.

### No-data area (`hrpns_nd`, checked 2026-10-13)
- Tiles cannot tell "no echo" from "not observed". JMA publishes the
  no-data area per frame as **GeoJSON**, not tiles:
  `.../jmatile/data/nowc/<basetime>/none/<validtime>/surf/hrpns_nd/data.geojson`
  (found in `bosai/nowc/table/nowc.properties__*.xml`, `imageType
  id="hrpns_nd" type="geojson"`; JMA's map veils it with a gray texture)
- One Polygon: the world as the exterior, the observed area as the hole.
  Served gzipped (~170 KB for an observation, ~40 KB for +60 min)
- Observation frames share one shape unless a radar goes down; the forecast
  shape changes with lead time, so it is fetched per frame like the tiles

### Implementation (`services/jma_radar_service.py`, `figures/radar_map.py`, `components/radar_view.py`)
- Areas: the whole country or a region (北日本・東日本・西日本・南西諸島),
  `_japan_map.AREAS`, shared by 図 and 動く地図. Each area names the tile
  zoom per mode (`figure_zoom`, `moving_zoom`)
- 図: z=6 (~2 km, 36 tiles) for the whole country, z=8 (~0.5 km, 30 – 56
  tiles) for a region. The figure is sized to the area and keeps the
  AMeDAS map's frame, land and coastline (`japan_axes(extent=…)`)
- Requests are paced to **4 per second** across frames, at most 4 in
  flight; a new figure frame takes 8 – 14 s, one on disk shows at once
- The user picks the latest observation (取得), any frame of the past 3 h,
  or the forecast; each pick fetches one frame and nothing else
- Tiles are decoded to intensity classes 0 (no echo) and 1 … 8 (JMA bins)
  by nearest legend colour; a colour far from the legend is logged as a
  palette change
- Each frame also brings its no-data polygons; both modes veil those cells
  in light gray (`NO_DATA_RGBA`) so "not observed" never reads as "no rain".
  On the GPU map they are class 9, read out as 観測範囲外・欠測
- Cache: tiles at `<data_dir>/jma/radar/<basetime>/<validtime>/<z>/<x>/<y>.png`,
  the no-data GeoJSON (gzipped) at `.../<basetime>/<validtime>/hrpns_nd.geojson.gz`;
  both immutable, deleted once their basetime is 2 hours old
- The figure resamples the Mercator mosaic onto the lat/lon frame, keeping
  the strongest class per figure pixel so small heavy-rain cores survive.
  The coastline is drawn over the radar
- Colours are JMA's (`JMA_PRECIP_COLOURS`, shared with the AMeDAS
  precipitation map). Do not invent a new scale; users recognise JMA's
- 動く地図 (`services/gpu_radar.py`, `components/radar_gpu_panel.py`): every
  frame of the past 1 or 3 hours and the forecast, on the GPU map. The whole
  country uses z=4 (~8 km, the tiles JMA's own nationwide animation shows),
  a region z=6 (~2 km); 4 – 9 tiles a frame plus its no-data GeoJSON,
  fetched latest observation → forecast → past and shown as they arrive.
  Classes are drawn with nearest sampling, never interpolated

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
the app; precipitation uses JMA's bins. The map frame, land and coastline
come from `figures/_japan_map.py`, shared with the radar map: Natural Earth
10m clipped to the frame (`figures/_japan_coastline.npz`, built once by
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

- Radar: `<data_dir>/jma/radar/_latest_meta.json` holds the time index
  (10-minute window); tiles at
  `<data_dir>/jma/radar/<basetime>/<validtime>/<z>/<x>/<y>.png` and the
  frame's no-data area `.../hrpns_nd.geojson.gz` (2 hours)
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
