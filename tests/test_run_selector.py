# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

from datetime import datetime, timezone

from aiseed_weather.services.run_selector import latest_available_run


def test_uses_six_hours_ago_when_just_past_run_publication():
    # at 06:10 UTC the 00z run has been out for 10 minutes; should be selected
    now = datetime(2026, 5, 14, 6, 10, tzinfo=timezone.utc)
    assert latest_available_run(now) == datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)


def test_falls_back_to_previous_run_before_publication():
    # at 05:59 UTC the 00z run is not yet published; should still pick yesterday 18z
    now = datetime(2026, 5, 14, 5, 59, tzinfo=timezone.utc)
    assert latest_available_run(now) == datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def test_picks_18z_after_midnight():
    now = datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc)
    assert latest_available_run(now) == datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
