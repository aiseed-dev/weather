"""世界の天気(worldtime-web 向け /data/world/ 配信)の取得・変換。

ソース:
    met.no locationforecast/2.0 compact … 都市別予報(CC BY 4.0、UA 識別必須)
    aviationweather.gov data API        … METAR 実測(米国政府、認証不要)

すべて標準ライブラリのみ。都市マスターは master/world_cities.json
(worldtime-web の tools/export_world_cities.py が生成)。
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "WeatherStaticWorld/0.1 (https://www.time-j.net; contact: saki@yniji.net)"
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json"
REQUEST_INTERVAL = 0.3  # 秒。met.no への連続リクエスト間隔
FORECAST_DAYS = 8
HOURLY_HOURS = 48


def http_get(url: str, *, retry: int = 1) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    for attempt in range(retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                body = res.read()
                if res.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except Exception:
            if attempt >= retry:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- met.no 予報

def fetch_metno(lat: float, lon: float) -> dict:
    url = METNO_URL.format(lat=round(lat, 4), lon=round(lon, 4))
    return json.loads(http_get(url))


def _symbol(data: dict) -> str:
    for key in ("next_1_hours", "next_6_hours", "next_12_hours"):
        s = data.get(key, {}).get("summary", {}).get("symbol_code")
        if s:
            return s
    return ""


def transform_forecast(raw: dict, city: dict, fetched: str) -> dict:
    """met.no compact JSON → 配信スキーマ(ソース中立)に変換する。

    hourly: 直近48時間(1時間または6時間刻み、APIの粒度のまま)
    daily : 現地日付で集計した8日分(気温min/max・降水量・正午前後の天気)
    """
    tz = ZoneInfo(city["tz"])
    series = raw["properties"]["timeseries"]
    t0 = datetime.fromisoformat(series[0]["time"])

    hourly = []
    days: dict[str, dict] = {}
    for entry in series:
        t = datetime.fromisoformat(entry["time"])
        data = entry["data"]
        inst = data.get("instant", {}).get("details", {})
        temp = inst.get("air_temperature")
        local = t.astimezone(tz)

        if (t - t0).total_seconds() <= HOURLY_HOURS * 3600:
            hourly.append({
                "t": t.isoformat().replace("+00:00", "Z"),
                "temp": temp,
                "sym": _symbol(data),
                "pre": (data.get("next_1_hours") or data.get("next_6_hours", {}))
                    .get("details", {}).get("precipitation_amount"),
                "wind": inst.get("wind_speed"),
                "wdir": inst.get("wind_from_direction"),
                "rh": inst.get("relative_humidity"),
            })

        d = days.setdefault(local.date().isoformat(), {
            "temps": [], "pre": 0.0, "sym": "", "sym_dist": 99})
        if temp is not None:
            d["temps"].append(temp)
        if "next_1_hours" in data:
            d["pre"] += data["next_1_hours"].get("details", {}) \
                .get("precipitation_amount") or 0.0
        elif "next_6_hours" in data:
            d["pre"] += data["next_6_hours"].get("details", {}) \
                .get("precipitation_amount") or 0.0
        # 現地正午に最も近い時点の天気を日代表にする
        dist = abs(local.hour - 12)
        sym = data.get("next_6_hours", {}).get("summary", {}).get("symbol_code") \
            or _symbol(data)
        if sym and dist < d["sym_dist"]:
            d["sym"], d["sym_dist"] = sym, dist

    daily = []
    for date_str in sorted(days)[:FORECAST_DAYS]:
        d = days[date_str]
        if not d["temps"]:
            continue
        daily.append({
            "date": date_str,
            "tmin": round(min(d["temps"]), 1),
            "tmax": round(max(d["temps"]), 1),
            "pre": round(d["pre"], 1),
            "sym": d["sym"],
        })

    return {
        "place": city["place"],
        "name": city["name"],
        "updated": raw["properties"]["meta"]["updated_at"],
        "fetched": fetched,
        "source": "MET Norway (CC BY 4.0)",
        "hourly": hourly,
        "daily": daily,
    }


# ---------------------------------------------------------------- METAR 実測

def fetch_metars(icaos: list[str], *, chunk: int = 100) -> dict[str, dict]:
    """複数 ICAO の最新 METAR をまとめて取得し、icao → 観測 dict で返す。

    通報の無い局は結果に含まれない(呼び出し側で欠落を許容する)。
    """
    result: dict[str, dict] = {}
    for i in range(0, len(icaos), chunk):
        ids = ",".join(icaos[i:i + chunk])
        rows = json.loads(http_get(AWC_METAR_URL.format(ids=ids), retry=1))
        for r in rows:
            icao = r.get("icaoId")
            if not icao:
                continue
            result[icao] = {
                "icao": icao,
                "time": r.get("reportTime"),
                "temp": r.get("temp"),
                "dewp": r.get("dewp"),
                "wdir": r.get("wdir"),
                "wspd_kt": r.get("wspd"),
                "wgst_kt": r.get("wgst"),
                "visib": r.get("visib"),
                "wx": r.get("wxString"),
                "clouds": r.get("clouds") or [],
                "flt_cat": r.get("fltCat"),
                "raw": r.get("rawOb"),
                "source": "aviationweather.gov",
            }
        time.sleep(REQUEST_INTERVAL)
    return result
