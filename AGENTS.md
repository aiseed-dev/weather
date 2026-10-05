# Agent guidance

Skills are located in `.agents/skills/`. They cover the desktop app
(`src/`). For the web product (`WeatherStatic/`) and the data
infrastructure, start from `docs/README.md`.

## Hard rule: read the skill before editing

If the change touches an area with a corresponding skill, **read
that skill before the first `Edit` / `Write` of the turn** and
report which skills you read in your reply, on one line, e.g.

> Skills read: aiseed-conventions, flet-declarative, flet-component-basics.

Skipping this has produced real bugs in this repo — Flet API
mismatches (`Dropdown.on_change` vs `on_select`, `FilePicker.on_result`
that doesn't exist in 0.85), storing `ft.Control` instances in
`use_ref`, holding form inputs in `Control.value` instead of
`use_state`, etc. Every one of these was preventable by reading
the skill first.

"Read" means a `Read` / `cat` tool call that loads the skill text
into the current session. Recalling it from general knowledge does
not count. If the session has been compacted since you read it,
read it again.

The selection table below tells you which skills apply to which
kind of change.

## Skill list (read in this order when several apply)

1. `aiseed-conventions` — Project-wide rules and audience model (always first)
2. `flet-declarative` — How to write Flet UI in this project
3. `flet-component-basics` — Flet 0.85+ entry point + @ft.component + hooks
4. `user-action-fetch` — When and how data is fetched (always relevant for views)
5. `first-run-setup` — `config.toml` loading and data source selection (there is no setup screen)
6. `ecmwf-data-access` — ECMWF Open Data + ERA5 (the data core)
7. `data-flow` — How fetch, GRIB cache, layer, and render connect in the map view
8. `climatology-analysis` — ERA5 climatology, anomalies, percentiles (the differentiator)
9. `jma-data-access` — JMA radar + AMeDAS (Japan nowcast)
10. `weather-rendering` — Synoptic-quality map rendering
11. `chart-base-design` — Layered chart design (base / data / isoline / pill); pairs with weather-rendering for the visual-structure side
12. `figure-export` — Publication-ready exports with provenance
13. `open-meteo-access` — Supporting point-forecast feature (lower priority)

## Skill selection per task type

| Task | Read |
|------|------|
| Config loading / data source selection | conventions, first-run-setup |
| Map view rendering (ECMWF) | conventions, flet-declarative, flet-component-basics, user-action-fetch, ecmwf-data-access, data-flow, weather-rendering, chart-base-design |
| Anomaly / climatology feature | conventions, flet-declarative, user-action-fetch, ecmwf-data-access, climatology-analysis, weather-rendering, chart-base-design |
| Radar / AMeDAS view | conventions, flet-declarative, user-action-fetch, jma-data-access, weather-rendering |
| Export to PNG/PDF | conventions, weather-rendering, figure-export |
| Point forecast view (Open-Meteo) | conventions, flet-declarative, flet-component-basics, user-action-fetch, open-meteo-access, chart-base-design |
| Pure UI layout with mocked data | conventions, flet-declarative, flet-component-basics |
| Backend service work | conventions + relevant data-access skill (+ data-flow for `forecast_service`) |
| New Flet app / new component file | flet-component-basics first, then flet-declarative |

Always read `aiseed-conventions`.
Any task involving a view that displays data must also read `user-action-fetch`.
Any task that touches `components/` or constructs Flet controls must read `flet-declarative` and (for new components) `flet-component-basics`.

## Priorities

The order matters for prioritization:

- **The user picks data sources** by editing
  `~/.config/aiseed-weather/config.toml` for ECMWF/ERA5/Open-Meteo;
  JMA is per-feature (no config key)
- **Data fetches only on user actions** (open view, press Refresh, change param)
- **ECMWF + ERA5 + rendering + export** is the product
- **JMA radar + AMeDAS** is the "Japan, right now" layer
- **Open-Meteo** is convenience, implement after the core works
- **Climatology** is what makes the tool unique — treat it as central, not as an add-on

## When in doubt

Read the skill. The two-or-three minute cost is far less than the cost of
implementing something against this project's conventions, or worse,
shipping a figure without proper attribution.
