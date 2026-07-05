"""気象庁公開データのクライアント。

取得するもの:
  1. 最新の気象データ CSV（今日の最高/最低気温・全地点。10 分毎更新）
  2. 天気予報 JSON（bosai API。office 単位）
  3. 日別平年値（etrn ページ。初回のみ取得してローカルにマスター化）

すべて標準ライブラリのみ。アクセスは User-Agent 明示・タイムアウト・
リトライ 1 回・呼び出し側で間隔を空ける。
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

USER_AGENT = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)"
TIMEOUT = 30

URL_MXTEM = "https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext00_rct.csv"
URL_MNTEM = "https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mntemsadext00_rct.csv"
# 各日 24 時の確定値（過去 7 日分保持。MMDD = 対象日）
URL_MXTEM_DAY = "https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext{mmdd}.csv"
URL_MNTEM_DAY = "https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mntemsadext{mmdd}.csv"
# アメダス map JSON（全地点の観測値。10 分毎ファイル、過去 7 日分保持）
URL_AMEDAS_MAP = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"
URL_FORECAST = "https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json"
URL_NORMALS = ("https://www.data.jma.go.jp/stats/etrn/view/nml_sfc_d.php"
               "?prec_no={prec_no}&block_no={block_no}&year=&month={month}&day=&view=")


# 気象庁サーバーへの負荷抑制: 全リクエストにプロセス共通の最小間隔を強制する
MIN_INTERVAL = 0.3
_last_request = 0.0


def http_get(url: str, *, retry: int = 1) -> bytes:
    global _last_request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retry + 1):
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                return res.read()
        except Exception:
            if attempt >= retry:
                raise
            time.sleep(2.0)
    raise RuntimeError("unreachable")


def _x10(s: str) -> int:
    """'24.8' → 248。空・非数値は -999（旧サイトの欠測値）。"""
    s = s.strip().replace("]", "").replace(")", "")
    if not s:
        return -999
    try:
        return int(round(float(s) * 10))
    except ValueError:
        return -999


# ---------------------------------------------------------------- 最新気象データ CSV

def _iso_date(cells: list[str], i: int) -> str:
    """cells[i]=年, [i+1]=月, [i+2]=日 → 'YYYY-MM-DD'（欠けは ''）。"""
    try:
        return f"{int(cells[i]):04d}-{int(cells[i + 1]):02d}-{int(cells[i + 2]):02d}"
    except (ValueError, IndexError):
        return ""


class RctRow:
    """最新気象データ CSV の 1 地点分。"""
    __slots__ = ("amedas", "pref", "name", "code", "now", "temp", "temp_time",
                 "quality", "year_record", "year_record_date",
                 "record", "record_date", "month_record", "month_record_date")

    def __init__(self, cells: list[str]):
        self.amedas = cells[0].strip()                    # アメダス観測所番号
        self.pref = cells[1].strip()                      # 都道府県（CSV 表記）
        self.name = re.sub(r"（.*?）", "", cells[2]).strip()  # 地点名（カナ括弧除去）
        self.code = int(cells[3]) if cells[3].strip() else 0  # 国際地点番号
        # 現在時刻（JST）。確定値（MMDD）ファイルは時・分が空（日全体の確定値）なので
        # 「その日の 24 時」として扱う。「24 時」表記も翌日 0 時に正規化する。
        hour = int(cells[7]) if cells[7].strip() else 24
        minute = int(cells[8]) if cells[8].strip() else 0
        self.now = (datetime(int(cells[4]), int(cells[5]), int(cells[6]), 0, minute)
                    + timedelta(hours=hour))
        self.temp = _x10(cells[9])                        # 今日の値 ×10
        try:
            self.quality = int(cells[10]) if cells[10].strip() else 0  # 品質情報（8=確定, 4=速報）
        except ValueError:
            self.quality = 0
        h, m = cells[11].strip(), cells[12].strip()       # 起時
        self.temp_time = f"{h}:{m}" if h and m else ""
        # 今年の記録（列 22）と起日（列 24-26）
        self.year_record = _x10(cells[21]) if len(cells) > 21 else -999
        self.year_record_date = _iso_date(cells, 23)
        # 観測史上 1 位（列 27）と起日（列 29-31）
        self.record = _x10(cells[26]) if len(cells) > 26 else -999
        self.record_date = _iso_date(cells, 28)
        # 当月 1 位（列 32）と起日（列 34-36）
        self.month_record = _x10(cells[31]) if len(cells) > 31 else -999
        self.month_record_date = _iso_date(cells, 33)


def fetch_rct(url: str) -> list[RctRow]:
    raw = http_get(url).decode("cp932", errors="replace")
    rows = []
    for cells in csv.reader(io.StringIO(raw)):
        if not cells or not cells[0].strip().isdigit():
            continue  # ヘッダ・空行
        try:
            rows.append(RctRow(cells))
        except (ValueError, IndexError):
            continue  # 不正行はスキップ
    return rows


def count_ge(rows: list[RctRow], threshold_x10: int) -> int:
    """今日の値が threshold 以上の地点数（猛暑日 350 / 真夏日 300 など）。"""
    return sum(1 for r in rows if r.temp != -999 and r.temp >= threshold_x10)


def count_lt(rows: list[RctRow], threshold_x10: int) -> int:
    """今日の値が threshold 未満の地点数（真冬日 0 / 冬日 0 など）。"""
    return sum(1 for r in rows if r.temp != -999 and r.temp < threshold_x10)


# ---------------------------------------------------------------- アメダス map JSON

def fetch_amedas_map(ts: datetime) -> dict[str, dict]:
    """アメダス map JSON（全地点の観測値）を取得する。

    ts は JST の観測時刻（10 分単位。通常は毎正時を指定）。
    戻り値: {アメダス番号: {'temp': ×10 or None, 'precip1h': ×10 or None, 'sun1h': ×10 or None}}
    値は品質フラグ 0（正常）のもののみ採用し、それ以外は None。
    """
    url = URL_AMEDAS_MAP.format(ts=ts.strftime("%Y%m%d%H%M%S"))
    payload = json.loads(http_get(url))

    def pick(entry, key):
        v = entry.get(key)
        if isinstance(v, list) and len(v) >= 2 and v[1] == 0 and v[0] is not None:
            return int(round(float(v[0]) * 10))
        return None

    out = {}
    for amedas, entry in payload.items():
        out[amedas] = {
            "temp": pick(entry, "temp"),
            "precip1h": pick(entry, "precipitation1h"),
            "sun1h": pick(entry, "sun1h"),
        }
    return out


# ---------------------------------------------------------------- 予報 JSON

class OfficeForecast:
    """office 1 つ分の短期予報。区域別の天気と、アメダス地点別の気温予報。"""

    def __init__(self, payload):
        short = payload[0]  # [0]=短期予報, [1]=週間予報
        self.report = datetime.fromisoformat(short["reportDatetime"])
        ts = short["timeSeries"]
        # 区域別 天気: area_code → [(date, weather, code), ...]
        self.weather_defs = [datetime.fromisoformat(t).date() for t in ts[0]["timeDefines"]]
        self.weathers: dict[str, list[tuple]] = {}
        for a in ts[0]["areas"]:
            entries = []
            for i, d in enumerate(self.weather_defs):
                we = a.get("weathers", [None] * len(self.weather_defs))
                entries.append((d, (we[i] or "").replace("　", " ") if i < len(we) else "",
                                a["weatherCodes"][i] if i < len(a["weatherCodes"]) else ""))
            self.weathers[a["area"]["code"]] = entries
        # 地点別 気温: amedas → {(date, hour): temp℃(int)}
        self.temps: dict[str, dict] = {}
        temp_series = ts[2] if len(ts) > 2 else {"timeDefines": [], "areas": []}
        tdefs = [datetime.fromisoformat(t) for t in temp_series["timeDefines"]]
        for a in temp_series["areas"]:
            d = {}
            for i, t in enumerate(tdefs):
                v = a["temps"][i] if i < len(a["temps"]) else ""
                if v not in ("", None):
                    d[(t.date(), t.hour)] = int(v)
            self.temps[a["area"]["code"]] = d

    def weather_on(self, area_code: str, day: date) -> tuple[str, str]:
        """指定区域の指定日の (天気, 天気コード)。区域が無ければ office 先頭区域で代替。"""
        entries = self.weathers.get(area_code)
        if entries is None and self.weathers:
            entries = next(iter(self.weathers.values()))
        for d, we, code in entries or []:
            if d == day:
                return we, code
        if entries:  # 指定日が無ければ先頭
            return entries[0][1], entries[0][2]
        return "-", "-"

    def max_temp_on(self, amedas: str, day: date) -> Optional[int]:
        """指定アメダス地点の指定日の最高気温予報（T09:00 の値）℃。"""
        d = self.temps.get(amedas)
        if d is None and self.temps:
            d = next(iter(self.temps.values()))
        return (d or {}).get((day, 9))

    def min_temp_on(self, amedas: str, day: date) -> Optional[int]:
        """指定アメダス地点の指定日の最低気温予報（T00:00 の値）℃。"""
        d = self.temps.get(amedas)
        if d is None and self.temps:
            d = next(iter(self.temps.values()))
        return (d or {}).get((day, 0))

    def first_weather_day(self) -> Optional[date]:
        return self.weather_defs[0] if self.weather_defs else None


def fetch_forecast(office: str) -> OfficeForecast:
    payload = json.loads(http_get(URL_FORECAST.format(office=office)))
    return OfficeForecast(payload)


# ---------------------------------------------------------------- 日別平年値

def fetch_daily_normals(prec_no: int, block_no: int, month: int) -> dict[int, dict]:
    """etrn の日別平年値ページから {日: {'tavg':×10, 'tmax':×10, 'tmin':×10}} を返す。"""
    url = URL_NORMALS.format(prec_no=prec_no, block_no=block_no, month=month)
    html = http_get(url).decode("utf-8", errors="replace")
    result: dict[int, dict] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if not cells or not re.fullmatch(r"\d+日", cells[0] or ""):
            continue
        day = int(cells[0].rstrip("日"))
        # 列: [日, 降水量, 平均気温, 最高気温, 最低気温, ...]
        result[day] = {
            "tavg": _x10(cells[2]) if len(cells) > 2 else -999,
            "tmax": _x10(cells[3]) if len(cells) > 3 else -999,
            "tmin": _x10(cells[4]) if len(cells) > 4 else -999,
        }
    if not result:
        raise ValueError(f"平年値の解析に失敗: prec_no={prec_no} block_no={block_no} month={month}")
    return result
