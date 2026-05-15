# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""User-controlled settings, loaded from a TOML config file.

The user chooses data sources by editing the config file before launching
the app. The app never writes user choices — it only writes the initial
template once, so the user has a starting point to edit. See the
`first-run-setup` skill.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir


class ForecastSource(str, Enum):
    NONE = "none"
    ECMWF_AWS = "ecmwf_aws"
    ECMWF_AZURE = "ecmwf_azure"
    ECMWF_GCP = "ecmwf_gcp"
    ECMWF_DIRECT = "ecmwf_direct"


class HistoricalSource(str, Enum):
    NONE = "none"
    ERA5_AWS = "era5_aws"
    ERA5_CDS = "era5_cds"


class PointForecastSource(str, Enum):
    NONE = "none"
    OPEN_METEO = "open_meteo"


@dataclass(frozen=True)
class UserSettings:
    forecast_source: ForecastSource = ForecastSource.NONE
    historical_source: HistoricalSource = HistoricalSource.NONE
    point_source: PointForecastSource = PointForecastSource.NONE
    reference_period_start: int = 1991
    reference_period_end: int = 2020
    accept_attribution: bool = False
    data_dir: str | None = None  # None → default user_cache_dir("aiseed-weather")

    def has_forecast(self) -> bool:
        return self.forecast_source != ForecastSource.NONE

    def has_historical(self) -> bool:
        return self.historical_source != HistoricalSource.NONE


def config_path() -> Path:
    return Path(user_config_dir("aiseed-weather")) / "config.toml"


def window_state_path() -> Path:
    """Sidecar JSON with the last-known window geometry.

    Kept separate from config.toml because (a) it mutates on every
    resize/move while config.toml is hand-edited and stable;
    (b) JSON is the natural format for a small machine-managed dict.
    Path: ``{user_config_dir}/window.json``.
    """
    return Path(user_config_dir("aiseed-weather")) / "window.json"


def load_window_state() -> dict:
    """Return the persisted window geometry, or {} if no file exists
    or it's unreadable. Caller applies sensible defaults."""
    import json

    path = window_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_window_state(state: dict) -> None:
    """Persist window geometry. Atomic write-then-rename so a crash
    mid-write doesn't leave a truncated file."""
    import json

    path = window_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def resolved_data_dir(settings: UserSettings) -> Path:
    """The on-disk root for caches and downloads.

    Honors the user's ``data_dir`` setting (e.g. an external SSD); falls
    back to ``user_cache_dir("aiseed-weather")`` if unset. ``~`` and
    ``$HOME`` are expanded so the config file can be filesystem-friendly.
    """
    if settings.data_dir:
        return Path(settings.data_dir).expanduser()
    from platformdirs import user_cache_dir
    return Path(user_cache_dir("aiseed-weather"))


_TEMPLATE = """\
# AIseed Weather configuration
#
# Edit this file to choose data sources, then restart the app.
# Default location on Linux: ~/.config/aiseed-weather/config.toml
# ($XDG_CONFIG_HOME is honored.)
#
# The app is a neutral viewer: it never picks sources for you. Leaving a
# field as "none" disables features that depend on it. JMA radar and
# AMeDAS work regardless of these settings.

# ---- Forecast (future grids, ECMWF Open Data) ----
# Options:
#   "none"          : operate in historical / nowcast-only mode
#   "ecmwf_gcp"     : ECMWF Open Data via Google Cloud  ← recommended
#                     (storage.googleapis.com edge-caches the bulk
#                     oper-fc.grib2 in Asia-Pacific; ~1-2 s/step
#                     from Japan, the only mirror that's actually
#                     usable from JP since we switched to per-step
#                     bulk downloads)
#   "ecmwf_aws"     : ECMWF Open Data via AWS S3 (eu-central-1)
#                     — slow from JP: ~24 s/step at the 150 MB
#                     bulk size, because the bucket is Frankfurt-
#                     only with no Asia edge.
#   "ecmwf_azure"   : ECMWF Open Data via Azure (West Europe);
#                     similar latency profile to AWS from JP.
#   "ecmwf_direct"  : ECMWF direct (data.ecmwf.int); 500-connection
#                     limit, often 403s anonymous traffic. Last
#                     resort only.
forecast_source = "none"

# ---- Historical (past grids, ERA5) ----
# Options:
#   "none"
#   "era5_aws"      : anonymous, ~5-day lag from real-time
#   "era5_cds"      : Copernicus CDS API (requires free account; not yet wired up)
historical_source = "none"

# ---- Point forecast (supporting view) ----
# Options:
#   "none"
#   "open_meteo"    : public API, free for personal use, CC-BY-4.0
point_source = "none"

# ---- Climatology reference period ----
# WMO standard normal is 1991-2020. Change only if you know why.
reference_period_start = 1991
reference_period_end = 2020

# ---- Attribution acceptance ----
# Exported figures always embed CC-BY-4.0 attribution. Set this to true to
# confirm you understand and will not remove attribution when sharing.
# Export buttons stay disabled until this is true.
accept_attribution = false

# ---- Data storage ----
# Cached downloads (ECMWF GRIB2, JMA tiles/snapshots, Open-Meteo JSON) live
# under this directory, organized by source:
#   <data_dir>/ecmwf/{YYYYMMDD}/{HH}z/{param}_{step}h.grib2
#   <data_dir>/jma/radar/...
#   <data_dir>/jma/amedas/...
#   <data_dir>/openmeteo/...
# Leave commented out to use ~/.cache/aiseed-weather ($XDG_CACHE_HOME honored).
# Set it explicitly if you want data on a different disk (e.g. an external
# SSD). Tilde and $HOME are expanded.
# data_dir = "/mnt/wxdata/aiseed-weather"
"""


def template() -> str:
    return _TEMPLATE


@dataclass(frozen=True)
class LoadResult:
    status: Literal["ok", "created", "invalid"]
    path: Path
    settings: UserSettings | None = None
    error: str | None = None


def load_or_init() -> LoadResult:
    """Load config.toml, writing the template once if it doesn't exist.

    Status meanings:
        ok       — config existed and parsed cleanly; ``settings`` is set
        created  — config did not exist; the template was just written
        invalid  — config exists but failed to parse / validate; ``error`` is set
    """
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE, encoding="utf-8")
        return LoadResult(status="created", path=path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return LoadResult(status="invalid", path=path, error=f"TOML parse error: {e}")
    try:
        settings = _from_mapping(raw)
    except (ValueError, TypeError) as e:
        return LoadResult(status="invalid", path=path, error=str(e))
    return LoadResult(status="ok", path=path, settings=settings)


_DEFAULTS = UserSettings()


def _enum_field(data: dict, key: str, enum_cls, default):
    value = data.get(key, default.value)
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = ", ".join(repr(m.value) for m in enum_cls)
        raise ValueError(
            f"{key}: {value!r} is not a valid value. Choose one of: {valid}"
        ) from exc


def _from_mapping(data: dict) -> UserSettings:
    raw_data_dir = data.get("data_dir")
    return UserSettings(
        forecast_source=_enum_field(
            data, "forecast_source", ForecastSource, _DEFAULTS.forecast_source,
        ),
        historical_source=_enum_field(
            data, "historical_source", HistoricalSource, _DEFAULTS.historical_source,
        ),
        point_source=_enum_field(
            data, "point_source", PointForecastSource, _DEFAULTS.point_source,
        ),
        reference_period_start=int(
            data.get("reference_period_start", _DEFAULTS.reference_period_start),
        ),
        reference_period_end=int(
            data.get("reference_period_end", _DEFAULTS.reference_period_end),
        ),
        accept_attribution=bool(
            data.get("accept_attribution", _DEFAULTS.accept_attribution),
        ),
        data_dir=str(raw_data_dir) if raw_data_dir else None,
    )
