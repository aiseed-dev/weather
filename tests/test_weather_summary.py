# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

from aiseed_weather.services.weather_summary import (
    daily_weather_code,
    hourly_display_code,
)

_HOURS = list(range(24))


def _day(codes, precip=None):
    return daily_weather_code(_HOURS, codes, precip or [0.0] * 24)


def test_single_night_drizzle_hour_does_not_spoil_a_clear_day():
    codes = [0] * 24
    codes[2] = 51
    precip = [0.0] * 24
    precip[2] = 0.1
    assert _day(codes, precip) == 0


def test_median_of_daytime_sky_rounds_down_on_even_count():
    # Daytime 06..17: five 2s and seven 3s -> median is 3.
    codes = [3] * 24
    for h in range(6, 11):
        codes[h] = 2
    assert _day(codes) == 3


def test_median_of_daytime_sky_picks_two_when_two_dominates():
    # Daytime 06..17: seven 2s and five 3s -> median is 2.
    codes = [3] * 24
    for h in range(6, 13):
        codes[h] = 2
    assert _day(codes) == 2


def test_even_count_median_takes_the_smaller_middle_value():
    # Six 2s and six 3s in the daytime: sorted(x)[(12-1)//2] is a 2.
    codes = [3] * 24
    for h in range(6, 12):
        codes[h] = 2
    assert _day(codes) == 2


def test_rainy_day_returns_largest_code_of_the_dominant_group():
    codes = [3] * 24
    for h in range(8, 12):
        codes[h] = 61
    codes[12] = 63
    for h in range(13, 17):
        codes[h] = 61
    codes[2] = 71
    precip = [0.0] * 24
    for h in range(8, 17):
        precip[h] = 0.6
    # Nine rain hours (61 x8, 63 x1) beat one snow hour; max of rain is 63.
    assert sum(precip) >= 1.0
    assert _day(codes, precip) == 63


def test_rain_vs_snow_four_rain_hours_one_snow_hour():
    codes = [3] * 24
    precip = [0.0] * 24
    for h in range(8, 12):
        codes[h] = 61
        precip[h] = 1.0
    codes[12] = 71
    precip[12] = 1.0
    assert _day(codes, precip) == 61


def test_tie_between_rain_and_snow_goes_to_snow():
    codes = [3] * 24
    codes[8] = 63
    codes[9] = 73
    precip = [0.0] * 24
    precip[8] = 1.0
    precip[9] = 1.0
    assert _day(codes, precip) == 73


def test_thunder_in_daytime_wins_even_without_rain_total():
    codes = [1] * 24
    codes[14] = 95
    assert _day(codes) == 95


def test_night_thunder_alone_does_not_count_as_thunder_day():
    codes = [1] * 24
    codes[2] = 96
    assert _day(codes) == 1


def test_fog_for_half_of_daytime_gives_fog():
    codes = [1] * 24
    for h in range(6, 12):
        codes[h] = 45
    assert _day(codes) == 45


def test_fog_below_half_is_ignored_for_the_median():
    codes = [2] * 24
    for h in range(6, 8):
        codes[h] = 45
    assert _day(codes) == 2


def test_partial_evening_day_judged_by_the_rows_it_has():
    assert daily_weather_code([18, 19, 20], [3, 3, 3], [0.0, 0.0, 0.0]) == 3


def test_all_none_codes_return_none():
    assert daily_weather_code([6, 7], [None, None], [0.0, 0.0]) is None


def test_precipitation_total_without_precip_code_returns_61():
    codes = [3] * 24
    precip = [0.0] * 24
    precip[10] = 0.6
    precip[11] = 0.6
    assert _day(codes, precip) == 61


def test_light_precip_codes_below_one_mm_read_as_cloud():
    codes = [61] * 24
    precip = [0.01] * 24
    assert _day(codes, precip) == 3


def test_scattered_drizzle_under_half_a_millimetre_is_not_a_rainy_day():
    # 8 drizzle hours of 0.2 mm add up to 1.6 mm, but no hour is wet.
    codes = [2] * 24
    precip = [0.0] * 24
    for h in range(4, 12):
        codes[h] = 51
        precip[h] = 0.2
    assert sum(precip) >= 1.0
    # Daytime 06..17: six drizzle hours read as 3, six 2s -> median 2.
    assert _day(codes, precip) == 2


def test_two_wet_rain_hours_make_a_rainy_day():
    codes = [3] * 24
    precip = [0.0] * 24
    for h in (9, 10):
        codes[h] = 61
        precip[h] = 0.6
    assert _day(codes, precip) == 61


def test_wet_drizzle_only_reads_as_light_rain():
    codes = [3] * 24
    precip = [0.0] * 24
    for h in (9, 10, 11):
        codes[h] = 53
        precip[h] = 0.5
    assert _day(codes, precip) == 61


def test_dry_hours_do_not_vote_for_the_precipitation_group():
    codes = [3] * 24
    precip = [0.0] * 24
    for h in (9, 10):
        codes[h] = 63
        precip[h] = 1.0
    codes[11] = 71
    precip[11] = 0.6
    assert _day(codes, precip) == 63


def test_hourly_display_code_hides_weak_precipitation():
    assert hourly_display_code(51, 0.1) == 3
    assert hourly_display_code(61, 0.5) == 61
    assert hourly_display_code(61, None) == 3
    assert hourly_display_code(95, 0.0) == 95
    assert hourly_display_code(45, 0.0) == 45
    assert hourly_display_code(1, 0.0) == 1
    assert hourly_display_code(None, 1.0) is None
