# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

from dataclasses import replace
from pathlib import Path

import pytest

from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import (
    ForecastSource,
    HistoricalSource,
    PointForecastSource,
    UserSettings,
)


@pytest.fixture
def temp_settings_path(tmp_path, monkeypatch):
    fake = tmp_path / "settings.json"
    monkeypatch.setattr(user_settings, "settings_path", lambda: fake)
    return fake


def test_default_settings_are_neutral():
    # The defaults must not pick any source; the user must choose.
    s = UserSettings()
    assert s.forecast_source == ForecastSource.NONE
    assert s.historical_source == HistoricalSource.NONE
    assert s.point_source == PointForecastSource.NONE
    assert s.setup_completed is False
    assert s.accepted_attribution_terms is False


def test_load_returns_defaults_when_file_missing(temp_settings_path):
    assert not temp_settings_path.exists()
    s = user_settings.load()
    assert s == UserSettings()


def test_save_then_load_roundtrip(temp_settings_path):
    chosen = UserSettings(
        forecast_source=ForecastSource.ECMWF_AWS,
        historical_source=HistoricalSource.ERA5_AWS,
        point_source=PointForecastSource.OPEN_METEO,
        setup_completed=True,
        accepted_attribution_terms=True,
    )
    user_settings.save(chosen)
    loaded = user_settings.load()
    assert loaded == chosen


def test_load_ignores_unknown_keys_for_forward_compatibility(temp_settings_path):
    # A settings file written by a newer version may include fields we don't know about.
    # We must not crash; we must silently ignore them and load what we recognize.
    temp_settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_settings_path.write_text(
        '{"forecast_source": "ecmwf_aws", "future_field_xyz": "value", '
        '"setup_completed": true, "accepted_attribution_terms": true}',
        encoding="utf-8",
    )
    s = user_settings.load()
    assert s.forecast_source == ForecastSource.ECMWF_AWS
    assert s.setup_completed is True
