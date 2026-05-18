# AIseed Weather

**A weather chart studio for enthusiasts and analysts.**
Build publication-ready figures from ECMWF, ERA5, JMA, and Open-Meteo, then
share them with the people who need to see them.

> "Bring Your Own Data" — this tool visualizes published meteorological data.
> It does not produce or distribute forecasts of its own.

> **日本語版**: see [README.md](README.md)

## Who this is for

This is **not** a general-purpose weather app. It is for people who:

- Read pressure charts, jet stream maps, and 500 hPa geopotential plots
- Want to compare current conditions against ERA5 climatology (1940-present)
- Need to check Japan's current radar and AMeDAS observations
- Need to produce annotated figures to explain weather events to others
- Find existing public charts too limited in variables or styling

If you do not know what MSL, geopotential height, or anomaly means, this tool
will not be friendlier than any other. That is by design — we optimize for
expert workflow, not onboarding.

## What it does

- **Global forecast maps** from ECMWF Open Data (IFS and AIFS)
- **Climatology / anomaly maps** from ERA5 (1940-present)
- **Japan rainfall nowcast** from JMA radar tiles
- **Japan ground observations** from JMA AMeDAS (~1,300 stations)
- **Multi-layer composition**: pressure isobars, temperature fields, wind,
  precipitation, geopotential at any pressure level
- **Annotation**: text labels, arrows, region highlights for explanation
- **Export**: PNG and PDF with embedded attribution and provenance metadata
- **Animation**: across forecast steps or historical date ranges
- **Point forecasts** from Open-Meteo as a supporting view

## Two principles you should know about

**1. The user chooses data sources.** Source selection lives in
`~/.config/aiseed-weather/config.toml`. The first launch writes a commented
template there for you to edit. Pick which ECMWF mirror, which ERA5 access
path, and whether to enable Open-Meteo, then restart. The app never
preselects and never shows a setup UI. JMA is per-feature (no config key
needed; JMA endpoints are public and free).

**2. Data fetches only on user actions.** Opening a view, pressing Refresh, or
changing a parameter triggers a fetch. The app never polls in the background,
never auto-updates a displayed value, never pre-fetches. If the cache is
fresh enough (per the source's update cadence), it is used; otherwise the
view shows a progress indicator and fetches.

## Status

Early development. Skeleton, services, conventions, and navigation are in
place. Next milestone: render a single MSL chart from a live ECMWF run.

## Stack

- [Flet](https://flet.dev/) — declarative Python UI
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
  via AWS S3 mirror (`s3://ecmwf-forecasts`) — primary data source
- [ERA5](https://registry.opendata.aws/ecmwf-era5/) via AWS (`s3://ecmwf-era5`)
  — climatology and historical reference
- [JMA](https://www.jma.go.jp/) public endpoints — Japan radar and AMeDAS
- [Open-Meteo](https://open-meteo.com/) — supporting point forecasts
- xarray + cfgrib for GRIB2 decoding
- matplotlib + cartopy for map rendering

## Setup (Miniforge required)

Cartopy, cfgrib, and eccodes depend on C libraries (PROJ, GEOS, eccodes).
Installing them via pip is painful per-platform, so we get them from
conda-forge.

### Step 1: Install Miniforge

Download the installer for your OS from
https://github.com/conda-forge/miniforge and run it.

```bash
# Linux / macOS
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash Miniforge3-$(uname)-$(uname -m).sh
```

Press Enter through the prompts. When asked "Do you wish to update your
shell profile to automatically initialize conda?", choose **yes**.
Restart your shell and verify with `mamba --version` or `conda --version`.

### Step 2: Disable automatic activation of the `base` env

Right after install, every new terminal automatically activates the `(base)`
environment. That mixes the system Python with conda's Python in confusing
ways, so disable it:

```bash
conda config --set auto_activate_base false
```

Now `base` no longer auto-loads. You activate environments explicitly only
when needed (`mamba activate <env-name>`).

### Step 3: Create the project-local virtual environment

AIseed Weather uses **a `.venv` inside the project directory** rather than
a globally-named environment. This keeps the global environment clean,
keeps the repo and its environment together, and makes moving to another
machine or integrating with VS Code straightforward.

```bash
# Clone the repository
git clone https://github.com//aiseed-weather.git
cd aiseed-weather

# Create .venv inside the project (Python 3.13 base)
mamba env create --prefix ./.venv -f environment.yml
```

The contents of `environment.yml` (C-library-dependent packages from
conda-forge, Flet via pip) install into `.venv`.

### Step 4: Activate the environment

```bash
mamba activate ./.venv
```

`--prefix` environments are activated by path, not by name. The prompt
should now show something like `(/path/to/aiseed-weather/.venv)`.

### Step 5: Editable install of the project (first time only)

```bash
pip install -e .
```

This makes `src/aiseed_weather/` importable from Python. `-e` (editable)
means file edits take effect without reinstall. The `src/` layout (PyPA
recommendation) requires this one-time step.

### Step 6: Launch

From the project root:

```bash
flet run
```

For development with hot-reload:

```bash
flet run -r
```

`pyproject.toml`'s `[tool.flet.app] path` setting tells `flet run` to find
`src/aiseed_weather/main.py` automatically from the project root.

### Analysis in JupyterLab

Sample notebooks are bundled under `notebooks/` — opening ECMWF GRIB2
directly, parallel Open-Meteo fetches, AMeDAS snapshots, and custom charts
using the project palette. With `./.venv` active:

```bash
jupyter lab notebooks/
```

See [`notebooks/README.en.md`](notebooks/README.en.md) for details.

### Updating or removing the environment

After editing `environment.yml`:

```bash
mamba env update --prefix ./.venv -f environment.yml --prune
```

`--prune` removes dependencies that were deleted from the yml.

To recreate the environment from scratch:

```bash
mamba env remove --prefix ./.venv
mamba env create --prefix ./.venv -f environment.yml
```

The `.venv` directory grows to several hundred MB or a few GB and is
excluded by `.gitignore`.

### Why Python 3.13?

Flet's mobile support relies on PEP 730 (iOS) and PEP 738 (Android), which
became official in Python 3.13.

### Why Flet via pip instead of conda?

Flet is in beta and releases frequently — conda-forge can lag. Installing
via pip lets us track the latest directly. Keeping Flet outside of conda's
view also avoids conda/pip resolution conflicts.

### Why `--prefix ./.venv` instead of a named environment?

A normal `mamba env create -n my-env` creates the env in conda's global
directory (`~/miniforge3/envs/`). `--prefix ./.venv` instead puts the env
inside the project, which gives you:

- Repo and env move together
- VS Code / PyCharm auto-detect `.venv`
- Deleting the project also removes the env
- No collisions across projects with similarly-named envs

### Windows and macOS

Official support is Linux (Debian/Ubuntu family). Development and testing
happen on Linux.

**macOS**: Install Miniforge for macOS and the steps above work as-is.
conda-forge has macOS builds of cartopy and cfgrib, so no extra work
needed.

**Windows**: Miniforge for Windows may work but is not officially tested.
If you want to run this as a serious desktop tool on Windows, consider
moving to Linux — Windows 10's end-of-support (October 2025) has been
prompting people to switch older PCs over.

WSL (Windows Subsystem for Linux) is **not** recommended for this app.
WSL is oriented toward CLI / server workloads; desktop GUI apps on WSL
suffer from rendering lag, file-dialog inconsistencies, and unstable
Japanese input.

## License

- Code: **AGPL-3.0-or-later**
- ECMWF data: CC-BY-4.0
- Open-Meteo data: CC-BY-4.0
- JMA data: 出典: 気象庁ホームページ — processed-data notice appears on
  composited figures (radar overlays, AMeDAS station maps)

The export feature automatically embeds attribution and the data run
identifier in every output, so figures shared from this tool carry their
provenance.

## Project layout

```
src/aiseed_weather/
├── main.py
├── components/                       # Flet components (UI only)
│   ├── app.py                        # nav between map / radar / amedas
│   ├── map_view.py                   # ECMWF/ERA5 synoptic charts
│   ├── radar_view.py                 # JMA rainfall nowcast
│   └── amedas_view.py                # JMA ground observations
├── services/                         # data fetching, decoding (no Flet imports)
│   ├── forecast_service.py           # ECMWF Open Data
│   ├── point_forecast_service.py     # Open-Meteo
│   ├── jma_radar_service.py          # JMA radar tiles
│   ├── jma_amedas_service.py         # JMA AMeDAS
│   └── jma_endpoints.py              # URL registry
└── models/                           # dataclasses, observable models
    └── user_settings.py
```

## For contributors and AI agents

Read `CLAUDE.md` first, then `AGENTS.md`, then the relevant skills under
`.agents/skills/`. The skills encode this project's conventions and the
prioritization between data sources.
