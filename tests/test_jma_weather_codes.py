# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import pytest

from aiseed_weather.services.jma_weather_codes import (
    _ICON_PARTS,
    _TELOPS,
    jma_weather,
)


def test_table_has_every_jma_code_and_every_icon_is_described():
    assert len(_TELOPS) == 118
    assert {icon for icon, _ in _TELOPS.values()} <= set(_ICON_PARTS)
    assert len(_ICON_PARTS) == 30


@pytest.mark.parametrize(
    ("code", "text", "main", "sub", "relation"),
    [
        (100, "晴", "sun", None, None),
        (201, "曇時々晴", "cloud", "sun", "時々"),
        (202, "曇一時雨", "cloud", "rain", "時々"),
        (212, "曇後一時雨", "cloud", "rain", "後"),
        (311, "雨後晴", "rain", "sun", "後"),
        (209, "霧", "cloud", None, None),
    ],
)
def test_known_codes(code, text, main, sub, relation):
    w = jma_weather(code)
    assert (w.text, w.main, w.sub, w.relation) == (text, main, sub, relation)


def test_unknown_code_falls_back_to_the_hundreds_digit():
    w = jma_weather(299)
    assert w.main == "cloud"
    assert w.text == "コード 299"
    assert w.sub is None and w.relation is None


def test_out_of_range_or_missing_code_is_none():
    assert jma_weather(999) is None
    assert jma_weather(None) is None
