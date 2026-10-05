---
name: ecmwf-data-access
description: How to fetch ECMWF Open Data (forecasts), ERA5 (climatology), and dynamical.org Zarr (ensembles). Read when modifying any service that talks to ECMWF data. This is the core data layer of the project.
---

## Why this skill matters

This project's value proposition is the **combination of ECMWF Open Data
(current/forecast) and ERA5 (climatology)** in one workflow. Both are first-
class data sources. Implement and test them as a pair, not as feature →
afterthought.

## Sources at a glance

| Source | Bucket | Use |
|--------|--------|-----|
| **ECMWF Open Data** | `s3://ecmwf-forecasts` (eu-central-1) | Current and forecast maps |
| **ERA5** | `s3://ecmwf-era5` (us-east-1) | Climatology, anomalies, historical maps |
| **dynamical.org IFS ENS** | `s3://dynamical-ecmwf-ifs-ens` (us-west-2) | Ensemble work, fast point time-series |
| ECMWF direct | `https://data.ecmwf.int/forecasts` | Fallback only (500 connection limit) |
| Azure mirror | `https://ai4edataeuwest.blob.core.windows.net/ecmwf` | Alternate for users with Azure preference |
| GCP mirror | `https://storage.googleapis.com/ecmwf-open-data` | Edge-cached; fastest measured from Japan |
| aiseed mirror | `mirror_url` in `config.toml` | Redistribution packs (NetCDF per kind), see `docs/forecast-distribution.md` |

All sources above are **anonymous (no account needed)**.

## ECMWF Open Data (real-time forecasts)

- Format: GRIB2 (`.grib2`) with `.index` files
- Resolutions: 0.25° (`0p25`) and 0.4°
- Models:
  - `ifs` — physics-based Integrated Forecasting System
  - `aifs` — Machine Learning model, often available earlier than IFS
- Streams: `oper` (high-res deterministic), `enfo` (ensemble), `wave`, `scda`
- Runs: 00, 06, 12, 18 UTC. Published ~6 hours after run time.
- Path: `s3://ecmwf-forecasts/YYYYMMDD/HHz/{resolution}/{stream}/<filename>.grib2`

### How the service downloads

`services/forecast_service.py` downloads the whole
`{date}{time}-{step}h-oper-fc.grib2` for each (cycle, step) with one HTTPS
GET from the base URL of the chosen source (`_BULK_BASE`). It does not use
`Client.retrieve` with `.index` Range requests: from Japan each Range
round-trip to Frankfurt cost ~250 ms, and one step took ~72 s versus
~1.3 s for the bulk file from GCP (timings in
`tests/test_download_bench.py`). The price is ~150 MB of disk per step.

`ecmwf-opendata`'s `Client` is still used to find the latest run
(`Client.latest()`), because the publication delay varies. The `mirror`
source reads `latest.json` from the mirror instead.

See the `data-flow` skill for how requests, kinds, and cache files relate.

### Variables that matter for synoptic charts

The user will expect these as named layers from day one:

| Short name | Long name | Typical level |
|-----------|-----------|---------------|
| `msl` | Mean sea level pressure | surface |
| `2t` | 2m temperature | surface |
| `10u`, `10v` | 10m wind components | surface |
| `tp` | Total precipitation | surface (accumulated) |
| `gh` | Geopotential height | 500, 850, 300 hPa |
| `t` | Temperature on pressure level | 850, 500 hPa |
| `u`, `v` | Wind on pressure level | 250 hPa (jet) |
| `q` | Specific humidity | 850, 700 hPa |
| `r` | Relative humidity | 850 hPa |

These are not "nice to have" — they are the building blocks of every
synoptic chart the user has ever read. Implement the layer dictionary so
adding a new one is one entry, not one PR.

### AIFS specifics

- ML-based; published faster than IFS for the same nominal run
- Use `model="aifs"` in the retrieve call
- Variable coverage is a subset of IFS — check before listing as a layer
- Worth offering as a parallel "model" toggle in the UI; users comparing
  AIFS to IFS is a real workflow

## ERA5 (the climatology engine)

ERA5 is what turns this from "another weather chart app" into something
unique. Treat it as a first-class subsystem, not an afterthought.

### Coverage

- 1940 → present (~85 years and counting)
- Global, 0.25° resolution
- Hourly, multiple pressure levels and surface fields
- Available on `s3://ecmwf-era5` as NetCDF

### Two access paths

1. **AWS bucket (default)**: `s3fs` + `xarray.open_dataset(engine="netcdf4")`.
   No registration. Faster from many regions.
2. **CDS API** (`cdsapi`): more flexible queries, requires free Copernicus
   account. Use only when AWS does not mirror the variable.

### Access pattern (AWS)

```python
import s3fs
import xarray as xr

fs = s3fs.S3FileSystem(anon=True)
# ERA5 file layout: era5-pds/zarr/.../<year>/<month>/data/<var>.zarr
# (Check the exact path convention in the bucket README — it has evolved.)
url = "s3://era5-pds/.../2025/05/data/air_temperature_at_2_metres.zarr"
ds = xr.open_zarr(s3fs.S3Map(url, s3=fs), consolidated=True)
```

The exact layout of ERA5 on AWS has shifted over time. Always check the
bucket's current README before hardcoding paths. Centralize path construction
in `services/era5_paths.py` so a layout change is one fix.

### Climatology computation

For "anomaly vs 1991-2020" (the WMO standard reference period):

```python
# Reference climatology: per-day-of-year, averaged across years.
ref = (
    historical_ds
    .sel(time=slice("1991-01-01", "2020-12-31"))
    .groupby("time.dayofyear")
    .mean("time")
)
anomaly = current_field - ref.sel(dayofyear=current_field["time"].dt.dayofyear)
```

Cache the climatology aggregation aggressively. For a fixed reference period
and variable, this is a one-time computation per machine.

### Important: WMO reference period

Default reference period is **1991-2020** (current WMO normal). Make this
user-configurable but never hide it — the figure must say which reference
period the anomaly is against.

## dynamical.org IFS ENS Zarr

For ensemble percentiles or single-point time series across many steps, this
re-distribution is dramatically faster than GRIB.

- Bucket: `s3://dynamical-ecmwf-ifs-ens` (us-west-2, no auth)
- Format: Icechunk Zarr (cloud-optimized, small chunks)
- Strength: fetching one location uses ~16 KiB total

```python
import xarray as xr
import fsspec

store = fsspec.get_mapper("s3://dynamical-ecmwf-ifs-ens", anon=True)
ds = xr.open_zarr(store, consolidated=True)
```

Trade-off: third-party redistribution. Pin a known-good version. ECMWF's own
GRIB bucket remains canonical for grids.

## Source selection rules

| Use case | Source |
|----------|--------|
| Current map view | ECMWF Open Data + AWS |
| Forecast map (next 10 days) | ECMWF Open Data + AWS |
| Climatology / anomaly overlay | ERA5 + AWS |
| Historical event review (e.g. 2018 typhoon) | ERA5 + AWS |
| Ensemble spread, percentiles | dynamical.org Zarr |
| AIFS vs IFS comparison | ECMWF Open Data with `model=` toggle |
| Single point full variable set (supporting view) | Open-Meteo (see `open-meteo-access`) |

Do **not** use Open-Meteo for the main map view. Do **not** use ECMWF GRIB
for a single lat/lon — bandwidth is wasted.

## Timing rules

- Open Data is published a few hours after each run time, with a
  variable delay. Resolve the latest run by probing the server
  (`latest_run`, `probe_cycle_complete`), not by a fixed offset
- AIFS often beats IFS for the same run — handle them as independent timelines
- ERA5 lag: roughly 5 days behind real time for the public release;
  "ERA5T" preliminary data is sooner. Document which one the user sees.

## Caching

All caches live under `data_dir` (`resolved_data_dir(settings)`; default
`user_cache_dir("aiseed-weather")`).

- GRIB: `<data_dir>/ecmwf/{YYYYMMDD}/{HH}z/{step}h.grib2` (one bulk file
  per step; built by `grib_cache_path`)
- Mirror packs: `<data_dir>/mirror/{YYYYMMDD}/{HH}z/{step:03d}h-{kind}-core.nc`
- ERA5 fetched fields: `<data_dir>/era5/`
- ERA5 climatology aggregations: `<data_dir>/climatology/`,
  key `{var}_{ref_start}_{ref_end}.nc`
- Zarr access is read-through; do not pre-cache, let the chunk store handle it
- Never re-download non-empty files
- "Force refresh" must be explicit; never bust the cache silently

## GRIB decoding

The bulk file mixes several `typeOfLevel` hypercubes (msl at meanSea,
2t at heightAboveGround, gh on isobaric levels, soil layers), so a plain
`xr.open_dataset(path, engine="cfgrib")` raises `DatasetBuildError`. Open
it through `forecast_service.decode_kind(path, kind)`, which uses
`cfgrib.open_datasets` and returns the dataset for one kind.

Wrap in `asyncio.to_thread`. Requires `eccodes` C library — install via
conda-forge (see `environment.yml`).

## Attribution (mandatory on every figure)

Every figure (UI display **and** exported file) must show:

- For ECMWF data: `"Data: ECMWF Open Data (run YYYYMMDD HHz {model}). CC-BY-4.0"`
- For ERA5: `"Data: ECMWF ERA5 reanalysis. CC-BY-4.0"`
- For anomalies: include the reference period:
  `"Anomaly vs 1991-2020 climatology (ERA5). CC-BY-4.0"`

Implement once in a shared `figures/footer.py` utility, applied to every
exported figure.

## Forbidden

- Hardcoded run dates
- Synchronous downloads in event handlers
- Importing `flet` in this directory
- Silent fallback between sources — let the user see the error and choose
- Exporting a figure without the data run identifier and license attribution
- Using Open-Meteo for the main map view
- Hardcoding ERA5 bucket paths outside `services/era5_paths.py`
