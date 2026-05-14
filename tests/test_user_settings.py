# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import tomllib

import pytest

from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import (
    ForecastSource,
    HistoricalSource,
    PointForecastSource,
    UserSettings,
)


@pytest.fixture
def fake_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(user_settings, "config_path", lambda: path)
    return path


def test_defaults_are_neutral():
    # The dataclass defaults must not pick any source; the user must choose
    # by editing the config file.
    s = UserSettings()
    assert s.forecast_source == ForecastSource.NONE
    assert s.historical_source == HistoricalSource.NONE
    assert s.point_source == PointForecastSource.NONE
    assert s.accept_attribution is False


def test_load_or_init_creates_template_when_missing(fake_config):
    assert not fake_config.exists()
    result = user_settings.load_or_init()
    assert result.status == "created"
    assert result.path == fake_config
    assert fake_config.exists()
    # The template must be parseable TOML and expose every user-editable key.
    parsed = tomllib.loads(fake_config.read_text(encoding="utf-8"))
    assert parsed["forecast_source"] == "none"
    assert parsed["historical_source"] == "none"
    assert parsed["point_source"] == "none"
    assert parsed["accept_attribution"] is False


def test_load_or_init_parses_valid_config(fake_config):
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    fake_config.write_text(
        "\n".join(
            [
                'forecast_source = "ecmwf_aws"',
                'historical_source = "era5_aws"',
                'point_source = "open_meteo"',
                "reference_period_start = 1981",
                "reference_period_end = 2010",
                "accept_attribution = true",
            ],
        ),
        encoding="utf-8",
    )
    result = user_settings.load_or_init()
    assert result.status == "ok"
    s = result.settings
    assert s.forecast_source == ForecastSource.ECMWF_AWS
    assert s.historical_source == HistoricalSource.ERA5_AWS
    assert s.point_source == PointForecastSource.OPEN_METEO
    assert s.reference_period_start == 1981
    assert s.reference_period_end == 2010
    assert s.accept_attribution is True


def test_load_or_init_reports_invalid_enum(fake_config):
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    fake_config.write_text('forecast_source = "ecmwf_typo"\n', encoding="utf-8")
    result = user_settings.load_or_init()
    assert result.status == "invalid"
    assert "forecast_source" in result.error
    assert "ecmwf_typo" in result.error


def test_load_or_init_reports_toml_syntax_error(fake_config):
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    # Unterminated string is a TOML parse error.
    fake_config.write_text('forecast_source = "ecmwf_aws\n', encoding="utf-8")
    result = user_settings.load_or_init()
    assert result.status == "invalid"
    assert "TOML" in result.error


def test_load_or_init_ignores_unknown_keys(fake_config):
    # Forward-compat: a future version may add keys; older code must still load.
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    fake_config.write_text(
        "\n".join(
            [
                'forecast_source = "ecmwf_aws"',
                'future_field_xyz = "value"',
            ],
        ),
        encoding="utf-8",
    )
    result = user_settings.load_or_init()
    assert result.status == "ok"
    assert result.settings.forecast_source == ForecastSource.ECMWF_AWS
