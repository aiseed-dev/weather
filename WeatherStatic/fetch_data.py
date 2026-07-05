#!/usr/bin/env python3
"""取得層ドライバ。現在値スナップショット data/ を生成する（DATA_CONTRACT v2.1）。

出力（毎回上書き。増えない）:
    data/today.csv        … 全地点の今日の最高・最低気温＋今年/史上/当月の記録（code キー）
    data/today_meta.json  … データ基準時刻・全国の地点数集計
    data/forecast.json    … 主要都市の天気・気温予報（code キー）

ソース:
    mdrr 最新値 CSV（mxtemsadext00_rct / mntemsadext00_rct）
    bosai 予報 JSON（office 単位）
    master/stations.json（アメダス番号 → code の解決）

失敗時は例外で停止し、既存の data/* は変更しない（前回値で generate 可能）。
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from weatherlib import jma

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MASTER = BASE / "master"

FETCH_INTERVAL = 0.2

TODAY_FIELDS = [
    "code", "amedas",
    "tmax", "tmax_at", "tmax_q", "tmin", "tmin_at", "tmin_q",
    "year_tmax", "year_tmax_date", "year_tmin", "year_tmin_date",
    "record_tmax", "record_tmax_date", "month_tmax", "month_tmax_date",
    "record_tmin", "record_tmin_date", "month_tmin", "month_tmin_date",
]


def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def v(x: int) -> str:
    """×10 整数 → CSV セル（欠測 -999 は空欄）。"""
    return "" if x == -999 else str(x)


def load_stations() -> dict:
    return json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- today

def build_today(a2c: dict[str, int]) -> tuple[list[dict], dict]:
    log("最高気温・最低気温 CSV を取得中...")
    mx = jma.fetch_rct(jma.URL_MXTEM)
    time.sleep(FETCH_INTERVAL)
    mn = jma.fetch_rct(jma.URL_MNTEM)
    log(f"  最高 {len(mx)} 地点 / 最低 {len(mn)} 地点")
    mxd = {r.amedas: r for r in mx}
    mnd = {r.amedas: r for r in mn}

    meta = {
        "source_time": max(r.now for r in mx + mn).isoformat(timespec="minutes"),
        "counts": {
            "moushobi": jma.count_ge(mx, 350),   # 猛暑日: 最高 35℃以上
            "manatsubi": jma.count_ge(mx, 300),  # 真夏日: 30℃以上
            "natsubi": jma.count_ge(mx, 250),    # 夏日: 25℃以上
            "mafuyubi": jma.count_lt(mx, 0),     # 真冬日: 最高 0℃未満
            "fuyubi": jma.count_lt(mn, 0),       # 冬日: 最低 0℃未満
            "nettaiya": jma.count_ge(mn, 250),   # 熱帯夜: 最低 25℃以上（暫定: 今朝の値）
        },
    }

    rows = []
    n_unmapped = 0
    for amedas in sorted(set(mxd) | set(mnd)):
        code = a2c.get(amedas)
        if code is None:
            n_unmapped += 1
            continue
        x, n = mxd.get(amedas), mnd.get(amedas)
        rows.append({
            "code": code, "amedas": amedas,
            "tmax": v(x.temp) if x else "", "tmax_at": x.temp_time if x else "",
            "tmax_q": x.quality if x else "",
            "tmin": v(n.temp) if n else "", "tmin_at": n.temp_time if n else "",
            "tmin_q": n.quality if n else "",
            "year_tmax": v(x.year_record) if x else "",
            "year_tmax_date": x.year_record_date if x else "",
            "year_tmin": v(n.year_record) if n else "",
            "year_tmin_date": n.year_record_date if n else "",
            "record_tmax": v(x.record) if x else "",
            "record_tmax_date": x.record_date if x else "",
            "month_tmax": v(x.month_record) if x else "",
            "month_tmax_date": x.month_record_date if x else "",
            "record_tmin": v(n.record) if n else "",
            "record_tmin_date": n.record_date if n else "",
            "month_tmin": v(n.month_record) if n else "",
            "month_tmin_date": n.month_record_date if n else "",
        })
    if n_unmapped:
        log(f"  注意: code 未解決でスキップ: {n_unmapped} 地点（build_master 再実行で解決）")
    rows.sort(key=lambda r: r["code"])
    return rows, meta


# ---------------------------------------------------------------- forecast

def expected_issuance(now: datetime) -> datetime:
    """現時点で最新のはずの予報発表時刻（5時・11時・17時）。"""
    for h in (17, 11, 5):
        if now.hour >= h:
            return now.replace(hour=h, minute=0, second=0, microsecond=0)
    y = now - timedelta(days=1)
    return y.replace(hour=17, minute=0, second=0, microsecond=0)


def forecast_is_fresh(now: datetime) -> bool:
    """既存の forecast.json が最新の発表分なら True（再取得を省略して負荷を抑える）。"""
    p = DATA / "forecast.json"
    if not p.exists():
        return False
    try:
        fc = json.loads(p.read_text(encoding="utf-8"))
        reported = datetime.fromisoformat(fc["reported"])
    except Exception:
        return False
    return reported >= expected_issuance(now)


def build_forecast(stations: dict) -> dict:
    mains = {int(code): rec for code, rec in stations["stations"].items()
             if rec.get("main")}
    offices = sorted({rec["office"] for rec in mains.values()})
    log(f"予報 JSON を取得中... ({len(offices)} offices)")
    fcs: dict[str, jma.OfficeForecast] = {}
    for office in offices:
        try:
            fcs[office] = jma.fetch_forecast(office)
        except Exception as e:
            log(f"  警告: office {office} の予報取得に失敗: {e}")
        time.sleep(FETCH_INTERVAL)
    if not fcs:
        raise RuntimeError("予報がひとつも取得できませんでした")

    # 対象日の決定: 「今日の最高気温予報」がまだ提供されていれば今日、
    # 17 時発表以降で今日分が消えていれば明日（旧サイトの CurrentPath "0"/"17" 相当）
    sample = next(iter(fcs.values()))
    today = datetime.now().date()
    has_today_max = any((today, 9) in d for d in sample.temps.values())
    target_label = "today" if has_today_max else "tomorrow"
    target = today if target_label == "today" else today + timedelta(days=1)

    out = {}
    reported = None
    tomorrow = today + timedelta(days=1)
    for code, rec in mains.items():
        fc = fcs.get(rec["office"])
        if fc is None:
            continue
        reported = reported or fc.report
        days = {}
        for day in (today, tomorrow):
            weather, wcode = fc.weather_on(rec["area"], day)
            days[day.isoformat()] = {
                "weather": weather,
                "wcode": str(wcode),
                "tmax": fc.max_temp_on(rec["amedas"], day),
                "tmin": fc.min_temp_on(rec["amedas"], day),
            }
        out[str(code)] = days
    return {
        "reported": reported.replace(tzinfo=None).isoformat(timespec="minutes"),
        "target_date": target.isoformat(),   # 参考情報（取得時点の最高気温予報の対象日）
        "target_label": target_label,
        "stations": out,                     # code → {"YYYY-MM-DD": {...}} ← 実日付キー
    }


# ---------------------------------------------------------------- current（現在の天気・気温）

def build_current(stations: dict) -> dict:
    """主要都市の「現在」: 気温はアメダス最新 10 分値（実測）、天気は推計気象分布。"""
    from weatherlib import suikei

    mains = {int(code): rec for code, rec in stations["stations"].items()
             if rec.get("main")}

    amedas_time, temps = suikei.latest_amedas_map()
    bt, vt, wthr_time = suikei.latest_wthr_time()
    sampler = suikei.WthrSampler(bt, vt)

    out = {}
    for code, rec in mains.items():
        w = sampler.weather_at(rec["lat"], rec["lon"])
        time.sleep(0.05)
        out[str(code)] = {
            "temp": temps.get(rec["amedas"]),
            "wthr": w,
            "wcode": suikei.WTHR_CODES.get(w) if w else None,
        }
    log(f"現在値: アメダス {amedas_time:%H:%M} / 推計天気 {wthr_time:%H時}"
        f"（タイル {sampler.tiles_fetched} 枚）")
    return {
        "amedas_time": amedas_time.isoformat(timespec="minutes"),
        "wthr_time": wthr_time.isoformat(timespec="minutes"),
        "stations": out,
    }


def main() -> int:
    started = time.monotonic()
    stations = load_stations()
    a2c = stations["index"]["amedas_to_code"]

    rows, meta = build_today(a2c)
    f = io.StringIO()
    w = csv.DictWriter(f, fieldnames=TODAY_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    write_atomic(DATA / "today.csv", f.getvalue())
    write_atomic(DATA / "today_meta.json",
                 json.dumps(meta, ensure_ascii=False, indent=1))
    log(f"data/today.csv を出力（{len(rows)} 地点）/ today_meta.json "
        f"(counts: {meta['counts']})")

    if forecast_is_fresh(datetime.now()):
        log("forecast.json は最新の発表分のため再取得を省略")
    else:
        fc = build_forecast(stations)
        write_atomic(DATA / "forecast.json", json.dumps(fc, ensure_ascii=False, indent=1))
        log(f"data/forecast.json を出力（{len(fc['stations'])} 都市, target={fc['target_label']}）")

    try:
        cur = build_current(stations)
        write_atomic(DATA / "current.json", json.dumps(cur, ensure_ascii=False, indent=1))
        log(f"data/current.json を出力（{len(cur['stations'])} 都市, "
            f"{time.monotonic() - started:.1f} 秒）")
    except Exception as e:
        log(f"警告: 現在値の取得に失敗（前回の current.json を維持）: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
