# AIseed Weather

A weather chart studio for enthusiasts and analysts who want to build
publication-ready figures and share them.

**Target user**: someone who already understands MSL, geopotential height,
anomalies, and reads pressure charts. We optimize for their workflow, not
for onboarding novices.

**Core value**: ECMWF Open Data for current/forecast + ERA5 for climatology,
combined into a single workflow with annotation and export. JMA radar and
AMeDAS provide the "Japan, right now" nowcast layer. This combination is
what makes the project worth building.

See **AGENTS.md** for skill locations and which skills to read per task.

This file, AGENTS.md, and `.agents/skills/` cover the desktop app (`src/`).
The same repository also holds the web product (`WeatherStatic/`) and the
data infrastructure (deb2 + Cloudflare). Their docs are indexed in
`docs/README.md`.

## Data layers (each has a distinct role)

| Layer | Source | Role |
|-------|--------|------|
| Forecast grids | ECMWF Open Data | Future / map view |
| Climatology / anomaly | ERA5 | Historical context, differentiator |
| Japan nowcast | JMA radar + AMeDAS | "Right now" in Japan |
| Point forecasts | Open-Meteo | Supporting per-location view |

## Two foundational design rules

1. **The user chooses data sources** by editing
   `~/.config/aiseed-weather/config.toml` before launching the app
   (ECMWF / ERA5 / Open-Meteo). The app does not preselect and never shows
   a setup UI. JMA is not in the config — its use is per-feature.
2. **Data fetches only on user actions** — opening a view, pressing Refresh,
   or changing a parameter. No background polling, no auto-update, no
   pre-fetch. See the `user-action-fetch` skill.

## Stack

- UI: Flet (Python, declarative components mode)
- **Primary data**: ECMWF Open Data. One bulk GRIB per (cycle, step) over
  HTTPS from the mirror the user picks in `config.toml` (GCP / AWS / Azure /
  ECMWF direct), or the aiseed redistribution packs
  (`forecast_source = "mirror"`, see `docs/forecast-distribution.md`)
- **Climatology core**: ERA5 via AWS (`s3://ecmwf-era5`). No ERA5 service
  exists yet; the point-forecast climatology currently comes from the
  Open-Meteo archive (`services/point_climatology.py`)
- **Japan nowcast**: JMA (`www.jma.go.jp/bosai/...`) — public, no key
- Ensemble / point extraction: dynamical.org Zarr (`s3://dynamical-ecmwf-ifs-ens`)
- Supporting point forecasts: Open-Meteo
- Processing: xarray + cfgrib (GRIB2), numpy
- Map rendering: numpy + PIL + contourpy (`figures/_layered_renderer.py`).
  cartopy is used only to precompute coastline masks
- Point-forecast chart: `flet.canvas` on screen, matplotlib for the PNG download
- Export: planned (see the `figure-export` skill)
- Packaging: **Miniforge / conda-forge** (not uv) — required for cartopy + cfgrib

## Design philosophy

- **Expert user assumed**: Use correct meteorological terminology without
  glossing or simplification. Labels, units, and projections follow conventions
  the user already knows.
- **The figure is the product**: Every view should be designed so it can be
  exported as a clean, attributable image.
- **Provenance is non-negotiable**: Every exported figure carries the data
  source, run timestamp, and license attribution.
- **Respect data publishers**: Cache aggressively, fetch only when the user
  asks, identify the app honestly in User-Agent strings.
- **ECMWF + ERA5 first**: Implement these before Open-Meteo features.

## Environment

The environment is created **inside the repository** as `./.venv`, not as a
named global environment. See `README.md` for the full rationale.

```bash
mamba env create --prefix ./.venv -f environment.yml
mamba activate ./.venv
```

`pyproject.toml` is for packaging metadata. `environment.yml` is the source
of truth for development.

## License

- Code: AGPL-3.0-or-later
- Data attribution (must appear in UI and every exported figure):
  - ECMWF data → CC-BY-4.0
  - Open-Meteo data → CC-BY-4.0
  - JMA data → 出典: 気象庁ホームページ (+ processed-data notice when composited)

## Before any task

Read AGENTS.md. Its selection table says which skills apply to the task.
Read those skills, and report which ones you read in one line before
starting.
