---
name: aiseed-conventions
description: Project-wide conventions. Read first before any implementation task in this repository.
---

## Audience and design philosophy

Target user: a weather enthusiast or analyst who already understands
meteorological terminology (MSL, geopotential, anomaly, hPa levels, ensemble
spread, etc.) and wants to produce figures to share with others.

This tool is a **neutral viewer**, not a forecast provider. The user chooses
which data sources to use by editing a TOML config file at
`~/.config/aiseed-weather/config.toml` before launching the app; the app
does not pick for them and does not push a default. There is no in-app
setup screen — see the `first-run-setup` skill. This is the foundational
design principle from which everything else follows.

A second foundational rule: **data is fetched only in response to user
actions**. The user opening a view, pressing Refresh, or changing a parameter
are the only fetch triggers. The app never polls in the background, never
pre-fetches, and never auto-updates a displayed value. See the
`user-action-fetch` skill for the full pattern.

Implications for all code and UI:

- **The user chose this data source.** Never bypass that choice. If a source
  is unavailable, surface the error to the user rather than silently
  switching to another.
- **Use correct technical terminology without glossing.** "Geopotential height
  at 500 hPa" not "upper-air pressure thing". The user prefers precise.
- **The figure is the product, not the UI.** Design map views so the chart
  area can be exported cleanly. Controls live around the edges and stay out
  of the figure.
- **Every output carries provenance.** Data source, run timestamp, projection,
  reference period (for anomalies), and license attribution must appear on
  exported figures. Implement this once in a shared "figure footer" utility,
  never per-view.
- **Reproducibility over polish.** A user who shares a figure may be asked
  "what data is this from?" — the figure itself must answer that.

## State management

- No global state classes. No Riverpod/Provider equivalents.
- Components are self-contained: receive what they need as args, communicate
  up via callbacks.
- Shared mutable models only when truly shared (multiple components observe
  the same data). Use `@ft.observable` for these.
- Never store `ft.Control` instances in fields or state. Store ids/enums and
  build controls during render.

## Code style

- Comments explain **why**, never **how**.
  - Bad: `# loop through layers`
  - Good: `# IFS publishes 0p25 grids ~6 hours after run time; clamp older runs.`
- No "captain obvious" comments.
- Function and variable names should be self-explanatory enough that no
  how-comment is needed.

## Architecture

- `services/` contains pure-Python classes that talk to ECMWF, files, the OS.
  **No Flet imports allowed in services.**
- `components/` contains Flet components only. They call services through
  async methods.
- `models/` contains plain dataclasses or `@ft.observable` models. No I/O.
- `figures/` (when added) contains pure matplotlib figure builders. They take
  data, return a `Figure`, and know nothing about Flet.

## Async

- Service methods that touch network or disk are `async def`.
- Long-running CPU work (GRIB decoding, plotting) goes through
  `asyncio.to_thread` to avoid blocking the UI.

## File header

Every `.py` source file starts with:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed
```

## Naming

- snake_case for functions and variables
- PascalCase for components (defined with `@ft.component`)
- Component file name matches component name in snake_case: `MapView` lives
  in `map_view.py`
- Meteorological variable names follow ECMWF short names where possible:
  `msl`, `t2m`, `u10`, `v10`, `tp`, `gh`, `t`, `q`. Do not rename to
  "pressure", "temperature" etc. in code — the short names are the standard
  the user reads.

## Units and conventions for displayed data

- Temperature: °C in display (convert from K on the way to the figure)
- Pressure: hPa (convert from Pa)
- Geopotential: gpm or dam at user's choice (default dam, matches synoptic
  chart conventions)
- Wind: m/s by default, with knot option for marine/aviation contexts
- Precipitation: mm
- Time: always show UTC and local side by side; never display local alone
  on a figure intended for sharing

## Forbidden

- `from xyz import *`
- Mutating globals
- Bare `except:`
- `print()` for diagnostics (use `logging`)
- Hardcoded paths (use `pathlib` + a config module)
- Exporting a figure without attribution and run identifier
- Translating ECMWF variable short names into "friendlier" English in code
- **Defaulting a data source before the user has chosen one** — the
  config template ships with every source set to `"none"`, and the app
  never substitutes another value
- **Silently switching data sources** if the user's chosen one fails — report
  the error and let the user decide
- **Auto-downloading at app start** before the user has explicitly chosen
  to fetch (loading the config does not trigger any data fetch)
- **Adding an in-app setup screen** — choices live in `config.toml`, not in
  a UI flow
