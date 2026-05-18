# JupyterLab Sample Notebooks

A starter set for poking at `aiseed-weather`'s service layer, cache,
and palette from JupyterLab. Runs in the same `./.venv` that
`flet run` uses.

> **日本語版**: see [README.md](README.md)

## Why JupyterLab?

`aiseed-weather` is a "studio for making weather charts via a GUI",
but its entire backend is Python's scientific-Python stack
(xarray / polars / cfgrib / cartopy / matplotlib). When you notice
something interesting in the GUI and want to **drill into it on the
spot**, you want a free-form analysis environment alongside —
that's what Jupyter provides.

- Open cached ECMWF GRIB2 files with
  `xarray.open_dataset(..., engine="cfgrib")` and run your own
  analysis
- Fetch Open-Meteo forecasts as polars DataFrames for arbitrary
  locations and compare
- Inspect the latest AMeDAS snapshot as a polars table
- Prototype custom charts with the project's own palette

This kind of freedom is impossible in an Electron-style app, and is
the central strength of building desktop apps in Python.

## Launching

With `./.venv` active:

```bash
jupyter lab notebooks/
```

(No extra setup needed if `flet run` already works. `jupyterlab`,
`ipykernel`, and `ipympl` are listed in `environment.yml`.)

## Notebook list

| # | File | What it covers |
|---|---|---|
| 01 | [`01-quickstart.ipynb`](01-quickstart.ipynb) | Environment check, locate `data_dir`, list saved locations |
| 02 | [`02-ecmwf-grib2.ipynb`](02-ecmwf-grib2.ipynb) | Open a cached ECMWF GRIB2 with `cfgrib`; plot MSL |
| 03 | [`03-point-forecast.ipynb`](03-point-forecast.ipynb) | Fetch 3 cities (Tokushima / Sapporo / Naha) in parallel via Open-Meteo and compare |
| 04 | [`04-jma-nowcast.ipynb`](04-jma-nowcast.ipynb) | AMeDAS snapshot + `nearest_stations`; build a current-conditions table for major cities |
| 05 | [`05-custom-chart.ipynb`](05-custom-chart.ipynb) | Use the project's palette LUT to draw your own chart |

They're sequenced as an intro, but each notebook stands alone — open
whichever section is relevant.

## Conventions

### Top-level `await`

The service layer (`open_meteo_forecast.fetch_forecast`,
`JmaAmedasService.fetch`, etc.) is all `async def`. Jupyter
supports top-level `await` natively, so you don't need to wrap
calls in `asyncio.run(...)`:

```python
result = await fetch_forecast(latitude=33.78, longitude=134.49, client=client)
```

### Data attribution

Every dataset here has source-attribution rules you must respect:

- **ECMWF Open Data** → CC-BY-4.0, label "Source: ECMWF Open Data, CC-BY-4.0"
- **Open-Meteo** → CC-BY-4.0, label "Source: Open-Meteo (https://open-meteo.com)"
- **JMA** → "Source: Japan Meteorological Agency" (mark composited
  / processed data as such)

When publishing or sharing notebook-generated figures, include
attribution. See the end of `05-custom-chart.ipynb` and
`figures/footer.py` in the main app for the project's preferred
footer pattern.

### Where is the cache?

`aiseed-weather`'s cache lives in one of:

- The `data_dir` from `~/.config/aiseed-weather/config.toml`, if you set it
- Otherwise `platformdirs.user_cache_dir("aiseed-weather")`
  (Linux: `~/.cache/aiseed-weather/`)

`01-quickstart.ipynb` resolves and prints the actual path via
`resolved_data_dir(settings)`.

## Troubleshooting

### "Cache is empty"

ECMWF / Open-Meteo caches are only populated when **the user
explicitly requests data** (no background fetch by design). Run the
app once with `flet run`, add a location and hit Refresh, or open
the map tab and Refresh — the cache will populate.

### `cfgrib` fails to open

The conda-forge environment includes `eccodes` (the C library
`cfgrib` needs). If you installed `cfgrib` via plain `pip`, eccodes
won't be present and decoding will fail. Recreate the env via
miniforge:

```bash
mamba env create --prefix ./.venv -f environment.yml
```

### `ipympl` interactive plots don't show

Put `%matplotlib widget` at the top of the cell. Restarting
JupyterLab sometimes helps it pick up the extension.

## Going deeper

- Each module under `src/aiseed_weather/services/` is designed to
  be usable standalone — read the docstrings for the public API
- `src/aiseed_weather/figures/` contains the project's chart
  conventions and palette specs
- `.agents/skills/` holds the coding-convention Skills that
  Claude Code auto-loads when working in this repo
