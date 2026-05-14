---
name: first-run-setup
description: How the first-run setup screen works and why it matters. Read when implementing or modifying the setup flow, data source selection, or settings persistence.
---

## Position and purpose

This tool is a **neutral viewer**. The app does not pick data sources for the
user — the user picks at first run. This is both a design principle (respect
the user's agency) and an operational one (the user is responsible for what
they choose to view).

The setup screen is the first thing the user sees. Get it right.

## What the setup screen does

1. Greets the user briefly (one sentence, no marketing)
2. Explains in plain language: "This app shows weather data from public
   sources. Choose which sources you want to use."
3. Presents the choices, each with:
   - Source name
   - Brief description of what it provides
   - License and attribution terms
   - A direct link to the source's documentation
4. Records the choice via `models.user_settings.save`
5. Confirms attribution terms acceptance (a single checkbox; required)
6. Lets the user proceed to the main app

The user can revisit settings later via a menu, but the first run is the
moment of explicit choice.

## What the setup screen does NOT do

- Does **not** preselect any source as "recommended" or "default"
- Does **not** auto-download anything before the user has chosen
- Does **not** call the data source APIs during setup (no probe requests)
- Does **not** hide the "no forecast / historical only" option
- Does **not** persist anything to disk until the user confirms

The point of the screen is to surface the choice, not to nudge.

## Source options to present

### Forecast (future grids)
- **None** — operate in historical/climatology mode only
- **ECMWF Open Data via AWS** — fastest globally, anonymous access
- **ECMWF Open Data via Azure** — alternate mirror
- **ECMWF Open Data via GCP** — often best from Asia-Pacific
- **ECMWF direct** — official endpoint, but 500-connection limit; use only if cloud mirrors fail

### Historical (past grids)
- **None**
- **ERA5 via AWS** — anonymous, ~5-day lag from real-time
- **ERA5 via CDS API** — requires free Copernicus registration, more flexible queries

### Point forecast (supporting view)
- **None**
- **Open-Meteo** — public API, free for personal use, CC-BY-4.0

Order matters: "None" comes first for each, signaling that no choice is required.

### JMA nowcast (radar, AMeDAS) — NOT in setup

JMA data is intentionally **not** part of first-run setup. JMA endpoints
require no credentials, no source selection, and have explicit usage terms
that apply per request. Using the JMA features inside the app is itself the
user's act of choosing to fetch from JMA — adding a setup toggle for it
would be unnecessary friction.

If we later add JMA cloud (paid) data sources, those would belong in setup.
The free public endpoints do not.

## Settings persistence

Settings live in `user_config_dir("aiseed-weather") / settings.json`.
Use the `models.user_settings` module — never write the file directly from
the UI layer.

The file is plain JSON for inspectability. The user can delete it to reset
the app to first-run state.

## Attribution acceptance

The user must explicitly check a box acknowledging:

> "I understand that data shown by this app is licensed under CC-BY-4.0.
> Figures I export include attribution automatically; I will not remove it
> when sharing."

This is recorded in `accepted_attribution_terms` and gates entry to the
main app. If the user unchecks it later in settings, sharing/export features
disable themselves.

## Re-entry to setup

The setup screen is reachable any time via Settings → "Data sources".
Changes there save immediately on confirmation, with a one-line summary of
what changed.

## Forbidden

- Defaulting any source to "selected" before the user chooses
- Hiding the "None" option in any category
- Making attribution acceptance a buried checkbox; it must be visible and explicit
- Skipping setup based on a "convenient default" branch
- Storing API keys or credentials in `settings.json` (use a separate secrets
  module if/when CDS API support is added)
- Calling data source APIs during setup just to "validate" the choice
