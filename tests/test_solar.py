# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

from datetime import datetime, timedelta, timezone

import pytest

from aiseed_weather.services.solar import is_daylight, solar_elevation_deg

JST = timezone(timedelta(hours=9))
TOKYO = (35.69, 139.69)


def test_tokyo_noon_is_day_and_midnight_is_night():
    assert is_daylight(datetime(2026, 10, 17, 12, tzinfo=JST), *TOKYO)
    assert not is_daylight(datetime(2026, 10, 17, 0, tzinfo=JST), *TOKYO)


def test_tokyo_sunrise_flips_around_0550_jst():
    before = datetime(2026, 10, 17, 5, 30, tzinfo=JST)
    after = datetime(2026, 10, 17, 6, 10, tzinfo=JST)
    assert not is_daylight(before, *TOKYO)
    assert is_daylight(after, *TOKYO)


def test_equator_equinox_noon_sun_is_overhead():
    # The equation of time is about -7 min in March, so solar noon at
    # lon 0 falls near 12:07 UTC rather than 12:00.
    noon = datetime(2026, 3, 20, 12, 7, tzinfo=timezone.utc)
    assert solar_elevation_deg(noon, 0.0, 0.0) == pytest.approx(90, abs=2)


@pytest.mark.parametrize("hour", range(0, 24, 3))
def test_north_pole_june_is_all_day(hour):
    when = datetime(2026, 6, 21, hour, tzinfo=timezone.utc)
    assert is_daylight(when, 90.0, 0.0)


@pytest.mark.parametrize("hour", range(0, 24, 3))
def test_north_pole_december_is_all_night(hour):
    when = datetime(2026, 12, 21, hour, tzinfo=timezone.utc)
    assert not is_daylight(when, 90.0, 0.0)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        solar_elevation_deg(datetime(2026, 10, 17, 12), *TOKYO)
