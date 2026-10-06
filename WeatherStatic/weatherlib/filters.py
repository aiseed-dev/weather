"""ViewUtility / Jma ヘルパーの Python 移植。

C# の Weather.Models.ViewUtility と Weather.Models.Jma のうち、
ビュー描画で使う関数だけを移植したもの。
気温は元の実装と同じく「整数 ×10（例: 25.3℃ → 253）」で保持する。
"""
from __future__ import annotations

from datetime import datetime, timedelta


# --- ViewUtility.ondo : 気温(×10)を "25.3℃" のような文字列にする -------------
def ondo(t: int | None) -> str:
    if t is None or t == -999:
        return "-"
    if t >= 0:
        return f"{t // 10}.{t % 10}℃"
    # 負の温度。C# 実装の分岐をそのまま踏襲する。
    if t // 10 == 0:            # -0.x のケース（-9〜-1）
        return f"-0.{-t}℃"
    return f"{-(-t // 10)}.{(-t) % 10}℃"


# --- ViewUtility.heinen : 平年差を表示 ---------------------------------------
def heinen(t: int | None, d: int | None) -> str:
    if t is None or d is None or t == -999 or d == -999:
        return "-"
    return ondo(t - d)


# --- ViewUtility.bcolor : 平年差(×10)から背景色を決める ----------------------
def bcolor(d: int) -> str:
    if d >= 500:
        return "#FFFFFF"
    if d >= 60:
        return "#FF786B"
    if d >= 40:
        return "#FFA584"
    if d >= 20:
        return "#FFE1D6"
    if d >= 0:
        return "#FFFFC2"
    if d >= -19:
        return "#E6FFFF"
    if d >= -39:
        return "#97FFFF"
    if d >= -59:
        return "#7ABEFF"
    if d >= -500:
        return "#8484FF"
    return "#FFFFFF"


# --- ViewUtility.時間表示 : "2024年7月5日14時" 形式。0時は前日24時扱い --------
def jikan(dt: datetime) -> str:
    if dt.hour == 0 and dt.minute == 0:
        d1 = dt - timedelta(hours=1)
        return f"{d1.year}年{d1.month}月{d1.day}日24時"
    # 今日の最高・最低は地点別 10 分値で 10 分ごとに更新されるので分まで出す
    minute = f"{dt.minute}分" if dt.minute else ""
    return f"{dt.year}年{dt.month}月{dt.day}日{dt.hour}時{minute}"


# --- Jma.JmaWeatherCodeToImage : 天気コード → 予報画像番号 --------------------
JMA_WEATHER_CODE_TO_IMAGE: dict[str, str] = {
    "100": "100", "101": "101", "102": "102", "103": "102", "104": "104",
    "105": "104", "106": "102", "107": "102", "108": "102", "110": "110",
    "111": "110", "112": "112", "113": "112", "114": "112", "115": "115",
    "116": "115", "117": "115", "118": "112", "119": "112", "120": "102",
    "121": "102", "122": "102", "123": "102", "124": "104", "125": "112",
    "126": "112", "127": "112", "128": "112", "129": "112", "130": "100",
    "131": "100", "132": "101", "140": "102", "160": "104", "170": "104",
    "181": "115", "200": "200", "201": "201", "202": "202", "203": "202",
    "204": "204", "205": "204", "206": "202", "207": "202", "208": "202",
    "209": "200", "210": "210", "211": "210", "212": "212", "213": "212",
    "214": "212", "215": "215", "216": "215", "217": "215", "218": "212",
    "219": "212", "220": "202", "221": "202", "222": "202", "223": "201",
    "224": "212", "225": "212", "226": "212", "227": "212", "228": "215",
    "229": "215", "230": "215", "231": "200", "240": "202", "250": "204",
    "260": "204", "270": "204", "281": "215", "300": "300", "301": "301",
    "302": "302", "303": "303", "304": "300", "306": "300", "308": "308",
    "309": "303", "311": "311", "313": "313", "314": "314", "315": "314",
    "316": "311", "317": "313", "320": "311", "321": "313", "322": "303",
    "323": "311", "324": "311", "325": "311", "326": "314", "327": "314",
    "328": "300", "329": "300", "340": "400", "350": "300", "361": "411",
    "371": "413", "400": "400", "401": "401", "402": "402", "403": "403",
    "405": "400", "406": "406", "407": "406", "409": "403", "411": "411",
    "413": "413", "414": "414", "420": "411", "421": "413", "422": "414",
    "423": "414", "424": "414", "425": "400", "426": "400", "427": "400",
    "450": "400",
}


def weather_img(code: str) -> str:
    """天気コードから予報アイコンのファイル名（拡張子なし手前の "s100" 等）を返す。"""
    return "s" + JMA_WEATHER_CODE_TO_IMAGE.get(str(code), "100")


# --- 気温(×10) → 服装の目安 ---------------------------------------------------
# 旧サイト Views/Stations/Clothes.cshtml の plotBands（7 段階）を移植
CLOTHES_BANDS = [
    (310, "熱中症に注意"),
    (255, "半袖の軽装"),
    (205, "長袖or半袖"),
    (155, "ジャケットorカーディガン"),
    (115, "ジャケット、セーターの重着"),
    (65, "コートが必要"),
    (-9999, "防寒対策を念入りに"),
]


def clothes(t: int | None) -> str:
    if t is None or t == -999:
        return ""
    for th, label in CLOTHES_BANDS:
        if t >= th:
            return label
    return ""


# --- ISO 日付 → "6月1日" -----------------------------------------------------
def jdate(s) -> str:
    if not s:
        return ""
    if isinstance(s, str):
        try:
            s = datetime.fromisoformat(s)
        except ValueError:
            return s
    return f"{s.month}月{s.day}日"


# Jinja2 環境へまとめて登録するための辞書
# 気温の色（値は ×10 の整数）。デスクトップアプリの気温の配色
# （src/aiseed_weather/figures/_chart_specs.py の T2M）と同じにして、
# サイトとアプリで同じ色が同じ気温を指すようにする。トップのスクリプトにも渡す
TEMP_ANCHORS = [
    (-200, (50, 30, 165)), (-100, (40, 115, 225)), (0, (230, 225, 215)),
    (100, (190, 220, 100)), (200, (245, 200, 80)), (300, (220, 110, 60)),
    (400, (135, 30, 30)),
]


# 平年差の色（値は ×10 の整数）。0 を淡い灰、高いほど赤・低いほど青。±6℃ で頭打ち。
# トップの地図（今の気温の平年差）のスクリプトに渡す
ANOM_ANCHORS = [
    (-60, (35, 80, 190)), (-30, (105, 160, 235)), (0, (232, 232, 226)),
    (30, (245, 160, 105)), (60, (200, 45, 40)),
]


def temp_color(t: int | None) -> str:
    if t is None or t == -999:
        return "transparent"
    if t <= TEMP_ANCHORS[0][0]:
        r, g, b = TEMP_ANCHORS[0][1]
    elif t >= TEMP_ANCHORS[-1][0]:
        r, g, b = TEMP_ANCHORS[-1][1]
    else:
        for (t0, c0), (t1, c1) in zip(TEMP_ANCHORS, TEMP_ANCHORS[1:]):
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0)
                r, g, b = (round(a + (z - a) * f) for a, z in zip(c0, c1))
                break
    return f"#{r:02x}{g:02x}{b:02x}"


FILTERS = {
    "ondo": ondo,
    "heinen": heinen,
    "bcolor": bcolor,
    "jikan": jikan,
    "jdate": jdate,
    "weather_img": weather_img,
    "clothes": clothes,
    "temp_color": temp_color,
}
