# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""User-controlled settings, persisted to disk.

The user decides which data sources to use at first run. The app itself is a
neutral viewer — it does not push a default forecast source or hide the choice.
This module owns the schema and the load/save round-trip.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path

from platformdirs import user_config_dir


class ForecastSource(str, Enum):
    """Where to pull forecast (future) grids from.

    NONE means the user has opted out of forecast data entirely — the app
    operates in pure historical/climatology mode.
    """

    NONE = "none"
    ECMWF_AWS = "ecmwf_aws"
    ECMWF_AZURE = "ecmwf_azure"
    ECMWF_GCP = "ecmwf_gcp"
    ECMWF_DIRECT = "ecmwf_direct"


class HistoricalSource(str, Enum):
    """Where to pull historical (past) grids from."""

    NONE = "none"
    ERA5_AWS = "era5_aws"
    ERA5_CDS = "era5_cds"


class PointForecastSource(str, Enum):
    """Where to pull supporting point-forecast data from."""

    NONE = "none"
    OPEN_METEO = "open_meteo"


@dataclass(frozen=True)
class UserSettings:
    forecast_source: ForecastSource = ForecastSource.NONE
    historical_source: HistoricalSource = HistoricalSource.NONE
    point_source: PointForecastSource = PointForecastSource.NONE
    reference_period_start: int = 1991
    reference_period_end: int = 2020
    setup_completed: bool = False
    accepted_attribution_terms: bool = False
    # Forward-compatibility: ignore unknown keys when loading, so an older
    # binary can still read a settings file written by a newer version.

    def has_forecast(self) -> bool:
        return self.forecast_source != ForecastSource.NONE

    def has_historical(self) -> bool:
        return self.historical_source != HistoricalSource.NONE


def settings_path() -> Path:
    return Path(user_config_dir("aiseed-weather")) / "settings.json"


def load() -> UserSettings:
    path = settings_path()
    if not path.exists():
        return UserSettings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(UserSettings)}
    filtered = {k: v for k, v in raw.items() if k in known}
    # Re-cast Enum strings to Enum instances.
    if "forecast_source" in filtered:
        filtered["forecast_source"] = ForecastSource(filtered["forecast_source"])
    if "historical_source" in filtered:
        filtered["historical_source"] = HistoricalSource(filtered["historical_source"])
    if "point_source" in filtered:
        filtered["point_source"] = PointForecastSource(filtered["point_source"])
    return UserSettings(**filtered)


def save(settings: UserSettings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        k: (v.value if isinstance(v, Enum) else v)
        for k, v in asdict(settings).items()
    }
    path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
