---
name: first-run-setup
description: How initial configuration works. The app reads ~/.config/aiseed-weather/config.toml and never shows a setup UI. Read when modifying config loading, data source selection, or related code.
---

## Position and purpose

This tool is a neutral viewer. The user chooses data sources by editing a
TOML config file before launching the app. There is no in-app setup
screen, no wizard, no nudging.

The first time the app runs without a config file, it writes a commented
template to `$XDG_CONFIG_HOME/aiseed-weather/config.toml` (default
`~/.config/aiseed-weather/config.toml`) and tells the user to edit it and
restart. This is the standard Linux pattern: dotfile, not wizard.

## What the app does on startup

1. Read config from `user_config_dir("aiseed-weather") / "config.toml"`.
2. If the file does not exist:
   - Create the parent directory.
   - Write the template returned by `user_settings.template()`.
   - Render a panel telling the user where the file is and to restart, in
     Japanese, with one button that opens the file in the OS text editor
     (`user_settings.open_in_text_editor`; Notepad on Windows, where `.toml`
     has no default program). The editor, not the app, touches the file.
3. If the file exists but fails to parse or validate:
   - Render a panel with the path and the reason. No fallback values.
4. Otherwise: hand the parsed `UserSettings` to the main app.

## What the app does NOT do

- Does **not** show a setup UI under any circumstance.
- Does **not** preselect any source as "recommended" or "default". The
  template ships with every source set to `"none"`.
- Does **not** write the config after the first template-creation. The
  user owns the file from that point on.
- Does **not** call any data source APIs during startup. The one thing sent
  is the usage report (`services/usage_report.py`): after the first paint,
  once a day, version + OS + build kind, nothing received. It is skipped on
  the run that creates the template (the user has not read the notice yet)
  and when `usage_report = false`. An existing config.toml without the key
  gets a dismissible notice on its first start instead (recorded as
  `notice_shown` in `usage.json`), and the report starts from the next
  start.
- Does **not** watch the file for changes — restart is the contract.

## Config schema

The schema lives in `models/user_settings.py`. Keep this table in sync
with the dataclass; do not introduce a second source of truth.

| Key | Type | Default | Valid values |
|-----|------|---------|--------------|
| `forecast_source` | string | `"none"` | `none`, `ecmwf_aws`, `ecmwf_azure`, `ecmwf_gcp`, `ecmwf_direct`, `mirror` |
| `historical_source` | string | `"none"` | `none`, `era5_aws`, `era5_cds` |
| `point_source` | string | `"none"` | `none`, `open_meteo` |
| `reference_period_start` | int | `1991` | year |
| `reference_period_end` | int | `2020` | year |
| `accept_attribution` | bool | `false` | `true` gates export features |
| `data_dir` | string \| omitted | omitted → `user_cache_dir("aiseed-weather")` | absolute path (e.g. `/mnt/wxdata/aiseed`); tilde and `$HOME` are expanded |
| `mirror_url` | string \| omitted | omitted | base URL of the aiseed redistribution packs; required when `forecast_source = "mirror"` (see `docs/forecast-distribution.md`) |
| `usage_report` | bool | `true` | `false` stops the once-a-day usage note (version + OS + build kind); anything but `true` / `false` is a config error |

## Data directory layout

All cached downloads live under `data_dir` (or the default user cache):

```
<data_dir>/
  ecmwf/{YYYYMMDD}/{HH}z/{step}h.grib2                 # ECMWF Open Data bulk GRIB2, all params per step
  mirror/{YYYYMMDD}/{HH}z/{step:03d}h-{kind}-core.nc   # redistribution packs (+ -ext*.nc siblings)
  jma/radar/...                                        # JMA radar tiles + meta
  jma/amedas/...                                       # AMeDAS snapshots + station table
  jma/forecast/...                                     # JMA forecast + area table
  observations/daily/manifest.json, years/, stations/  # JMA daily records since 1880 (redistributed NetCDF)
  openmeteo/...                                        # Open-Meteo JSON cache
  point_forecast/locations.json, archive/...           # saved locations + per-location Parquet archive
```

The path logic lives in `services/forecast_service.grib_cache_path`.
The hierarchical layout gathers a single run under one directory, which
scales much better than a flat folder when many runs × many steps are
cached.

JMA radar and AMeDAS are intentionally not in the config — JMA endpoints
need no credentials and using the feature is itself the act of choosing
to fetch.

## Attribution acceptance

The user records consent to CC-BY-4.0 attribution by setting
`accept_attribution = true`. Until they do, export features stay
disabled. Exports themselves always embed attribution; the flag gates
whether the export buttons are usable, not whether attribution is
written.

## Re-entry

To change sources, the user edits the config and restarts. There is no
"reload config" command; restart is intentional friction that mirrors
the "no background activity" rule.

## Resetting

Deleting `~/.config/aiseed-weather/config.toml` brings the app back to
the template-creation path on the next launch.

## Forbidden

- A first-run UI of any kind. The config is the contract.
- Reading or writing the file from the UI layer; only
  `models/user_settings` touches disk.
- Calling source APIs during load (no probe, no validation request).
- Storing secrets in `config.toml` (use a separate secrets module if/when
  CDS support adds an API key).
- Hiding the `"none"` value in any comment or table; `"none"` is always
  valid.
- Defining the schema in two places. The dataclass is canonical; the
  template string and this table follow it.
