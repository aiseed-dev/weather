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
   - Render a panel telling the user where the file is and to restart.
3. If the file exists but fails to parse or validate:
   - Render a panel with the path and the reason. No fallback values.
4. Otherwise: hand the parsed `UserSettings` to the main app.

## What the app does NOT do

- Does **not** show a setup UI under any circumstance.
- Does **not** preselect any source as "recommended" or "default". The
  template ships with every source set to `"none"`.
- Does **not** write the config after the first template-creation. The
  user owns the file from that point on.
- Does **not** call any data source APIs during startup.
- Does **not** watch the file for changes — restart is the contract.

## Config schema

The schema lives in `models/user_settings.py`. Keep this table in sync
with the dataclass; do not introduce a second source of truth.

| Key | Type | Default | Valid values |
|-----|------|---------|--------------|
| `forecast_source` | string | `"none"` | `none`, `ecmwf_aws`, `ecmwf_azure`, `ecmwf_gcp`, `ecmwf_direct` |
| `historical_source` | string | `"none"` | `none`, `era5_aws`, `era5_cds` |
| `point_source` | string | `"none"` | `none`, `open_meteo` |
| `reference_period_start` | int | `1991` | year |
| `reference_period_end` | int | `2020` | year |
| `accept_attribution` | bool | `false` | `true` gates export features |
| `data_dir` | string \| omitted | omitted → `user_cache_dir("aiseed-weather")` | absolute path (e.g. `/mnt/wxdata/aiseed`); tilde and `$HOME` are expanded |

## Data directory layout

All cached downloads live under `data_dir` (or the default user cache):

```
<data_dir>/
  ecmwf/{YYYYMMDD}/{HH}z/{param}_{step}h.grib2   # ECMWF Open Data GRIB2
  jma/radar/...                                  # JMA radar tiles + meta
  jma/amedas/...                                 # AMeDAS snapshots + station table
  openmeteo/...                                  # Open-Meteo JSON cache
```

The hierarchical ECMWF layout means a single run gathers all its fields
under one directory, which scales much better than a flat `grib/` folder
when many runs × many params × many steps are cached.

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
