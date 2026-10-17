# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA forecast weather codes (100, 201, 313 …) → wording and icon parts.

JMA's codes are not WMO numbers: the hundreds digit is only the broad
class, and the rest says "時々" (mixed through the day) or "後" (changes
later). Collapsing them to four classes made 201 (曇時々晴) and 202
(曇一時雨) both read as plain 曇り, so the table JMA itself uses is kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JmaWeather:
    text: str
    main: str  # "sun" | "cloud" | "rain" | "snow"
    sub: str | None
    # "時々": the two mix through the day. "後": main turns into sub.
    relation: str | None


# TELOPS table of https://www.jma.go.jp/bosai/forecast/ (taken 2026-10-17).
# code -> (daytime icon number, JMA wording). JMA reuses one icon for
# several codes (103 晴時々雨 draws the 102 icon).
_TELOPS: dict[int, tuple[int, str]] = {
    100: (100, "晴"),
    101: (101, "晴時々曇"),
    102: (102, "晴一時雨"),
    103: (102, "晴時々雨"),
    104: (104, "晴一時雪"),
    105: (104, "晴時々雪"),
    106: (102, "晴一時雨か雪"),
    107: (102, "晴時々雨か雪"),
    108: (102, "晴一時雨か雷雨"),
    110: (110, "晴後時々曇"),
    111: (110, "晴後曇"),
    112: (112, "晴後一時雨"),
    113: (112, "晴後時々雨"),
    114: (112, "晴後雨"),
    115: (115, "晴後一時雪"),
    116: (115, "晴後時々雪"),
    117: (115, "晴後雪"),
    118: (112, "晴後雨か雪"),
    119: (112, "晴後雨か雷雨"),
    120: (102, "晴朝夕一時雨"),
    121: (102, "晴朝の内一時雨"),
    122: (112, "晴夕方一時雨"),
    123: (100, "晴山沿い雷雨"),
    124: (100, "晴山沿い雪"),
    125: (112, "晴午後は雷雨"),
    126: (112, "晴昼頃から雨"),
    127: (112, "晴夕方から雨"),
    128: (112, "晴夜は雨"),
    130: (100, "朝の内霧後晴"),
    131: (100, "晴明け方霧"),
    132: (101, "晴朝夕曇"),
    140: (102, "晴時々雨で雷を伴う"),
    160: (104, "晴一時雪か雨"),
    170: (104, "晴時々雪か雨"),
    181: (115, "晴後雪か雨"),
    200: (200, "曇"),
    201: (201, "曇時々晴"),
    202: (202, "曇一時雨"),
    203: (202, "曇時々雨"),
    204: (204, "曇一時雪"),
    205: (204, "曇時々雪"),
    206: (202, "曇一時雨か雪"),
    207: (202, "曇時々雨か雪"),
    208: (202, "曇一時雨か雷雨"),
    209: (200, "霧"),
    210: (210, "曇後時々晴"),
    211: (210, "曇後晴"),
    212: (212, "曇後一時雨"),
    213: (212, "曇後時々雨"),
    214: (212, "曇後雨"),
    215: (215, "曇後一時雪"),
    216: (215, "曇後時々雪"),
    217: (215, "曇後雪"),
    218: (212, "曇後雨か雪"),
    219: (212, "曇後雨か雷雨"),
    220: (202, "曇朝夕一時雨"),
    221: (202, "曇朝の内一時雨"),
    222: (212, "曇夕方一時雨"),
    223: (201, "曇日中時々晴"),
    224: (212, "曇昼頃から雨"),
    225: (212, "曇夕方から雨"),
    226: (212, "曇夜は雨"),
    228: (215, "曇昼頃から雪"),
    229: (215, "曇夕方から雪"),
    230: (215, "曇夜は雪"),
    231: (200, "曇海上海岸は霧か霧雨"),
    240: (202, "曇時々雨で雷を伴う"),
    250: (204, "曇時々雪で雷を伴う"),
    260: (204, "曇一時雪か雨"),
    270: (204, "曇時々雪か雨"),
    281: (215, "曇後雪か雨"),
    300: (300, "雨"),
    301: (301, "雨時々晴"),
    302: (302, "雨時々止む"),
    303: (303, "雨時々雪"),
    304: (300, "雨か雪"),
    306: (300, "大雨"),
    308: (308, "雨で暴風を伴う"),
    309: (303, "雨一時雪"),
    311: (311, "雨後晴"),
    313: (313, "雨後曇"),
    314: (314, "雨後時々雪"),
    315: (314, "雨後雪"),
    316: (311, "雨か雪後晴"),
    317: (313, "雨か雪後曇"),
    320: (311, "朝の内雨後晴"),
    321: (313, "朝の内雨後曇"),
    322: (303, "雨朝晩一時雪"),
    323: (311, "雨昼頃から晴"),
    324: (311, "雨夕方から晴"),
    325: (311, "雨夜は晴"),
    326: (314, "雨夕方から雪"),
    327: (314, "雨夜は雪"),
    328: (300, "雨一時強く降る"),
    329: (300, "雨一時みぞれ"),
    340: (400, "雪か雨"),
    350: (300, "雨で雷を伴う"),
    361: (411, "雪か雨後晴"),
    371: (413, "雪か雨後曇"),
    400: (400, "雪"),
    401: (401, "雪時々晴"),
    402: (402, "雪時々止む"),
    403: (403, "雪時々雨"),
    405: (400, "大雪"),
    406: (406, "風雪強い"),
    407: (406, "暴風雪"),
    409: (403, "雪一時雨"),
    411: (411, "雪後晴"),
    413: (413, "雪後曇"),
    414: (414, "雪後雨"),
    420: (411, "朝の内雪後晴"),
    421: (413, "朝の内雪後曇"),
    422: (414, "雪昼頃から雨"),
    423: (414, "雪夕方から雨"),
    425: (400, "雪一時強く降る"),
    426: (400, "雪後みぞれ"),
    427: (400, "雪一時みぞれ"),
    450: (400, "雪で雷を伴う"),
}

# daytime icon number -> (main, sub, relation)
_ICON_PARTS: dict[int, tuple[str, str | None, str | None]] = {
    100: ("sun", None, None),
    101: ("sun", "cloud", "時々"),
    102: ("sun", "rain", "時々"),
    104: ("sun", "snow", "時々"),
    110: ("sun", "cloud", "後"),
    112: ("sun", "rain", "後"),
    115: ("sun", "snow", "後"),
    200: ("cloud", None, None),
    201: ("cloud", "sun", "時々"),
    202: ("cloud", "rain", "時々"),
    204: ("cloud", "snow", "時々"),
    210: ("cloud", "sun", "後"),
    212: ("cloud", "rain", "後"),
    215: ("cloud", "snow", "後"),
    300: ("rain", None, None),
    301: ("rain", "sun", "時々"),
    302: ("rain", "cloud", "時々"),
    303: ("rain", "snow", "時々"),
    308: ("rain", None, None),
    311: ("rain", "sun", "後"),
    313: ("rain", "cloud", "後"),
    314: ("rain", "snow", "後"),
    400: ("snow", None, None),
    401: ("snow", "sun", "時々"),
    402: ("snow", "cloud", "時々"),
    403: ("snow", "rain", "時々"),
    406: ("snow", None, None),
    411: ("snow", "sun", "後"),
    413: ("snow", "cloud", "後"),
    414: ("snow", "rain", "後"),
}

_MAIN_BY_HUNDREDS = {1: "sun", 2: "cloud", 3: "rain", 4: "snow"}

_warned_codes: set[int] = set()


def jma_weather(code: int | None) -> JmaWeather | None:
    if code is None:
        return None
    code = int(code)
    known = _TELOPS.get(code)
    if known is not None:
        icon, text = known
        main, sub, relation = _ICON_PARTS[icon]
        return JmaWeather(text=text, main=main, sub=sub, relation=relation)

    main = _MAIN_BY_HUNDREDS.get(code // 100)
    if main is None:
        return None
    # JMA adds codes now and then; draw the broad class rather than nothing.
    if code not in _warned_codes:
        _warned_codes.add(code)
        logger.info("JMA weather code %s is not in the TELOPS table", code)
    return JmaWeather(text=f"コード {code}", main=main, sub=None, relation=None)
