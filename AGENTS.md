# Agent guidance

Skills are located in `.agents/skills/`.

## Read order (every task)

1. `aiseed-conventions` — Project-wide rules and audience model (always first)
2. `flet-declarative` — How to write Flet UI in this project
3. `user-action-fetch` — When and how data is fetched (always relevant for views)
4. `first-run-setup` — Setup screen and data source selection rules
5. `ecmwf-data-access` — ECMWF Open Data + ERA5 (the data core)
6. `climatology-analysis` — ERA5 climatology, anomalies, percentiles (the differentiator)
7. `jma-data-access` — JMA radar + AMeDAS (Japan nowcast)
8. `weather-rendering` — Synoptic-quality map rendering
9. `figure-export` — Publication-ready exports with provenance
10. `open-meteo-access` — Supporting point-forecast feature (lower priority)

## Skill selection per task type

| Task | Read |
|------|------|
| Setup screen / settings UI | conventions, flet, first-run-setup |
| Map view rendering (ECMWF) | conventions, flet, user-action-fetch, ecmwf-data-access, weather-rendering |
| Anomaly / climatology feature | conventions, flet, user-action-fetch, ecmwf-data-access, climatology-analysis, weather-rendering |
| Radar / AMeDAS view | conventions, flet, user-action-fetch, jma-data-access, weather-rendering |
| Export to PNG/PDF | conventions, weather-rendering, figure-export |
| Point forecast (supporting view) | conventions, flet, user-action-fetch, open-meteo-access |
| Pure UI layout with mocked data | conventions, flet |
| Backend service work | conventions + relevant data-access skill |

Always read `aiseed-conventions` and `flet-declarative`.
Any task involving a view that displays data must also read `user-action-fetch`.

## Priorities

The order matters for prioritization:

- **The user picks data sources** at first run for ECMWF/ERA5/Open-Meteo;
  JMA is per-feature (no setup toggle)
- **Data fetches only on user actions** (open view, press Refresh, change param)
- **ECMWF + ERA5 + rendering + export** is the product
- **JMA radar + AMeDAS** is the "Japan, right now" layer
- **Open-Meteo** is convenience, implement after the core works
- **Climatology** is what makes the tool unique — treat it as central, not as an add-on

## When in doubt

Read the skill. The two-or-three minute cost is far less than the cost of
implementing something against this project's conventions, or worse,
shipping a figure without proper attribution.
