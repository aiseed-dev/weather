#!/usr/bin/env python3
"""手元（dev）で site を生成して見た目を確かめるための、作り物の観測データを置く。

なぜ要るか
    実データは dev2 にしかない。手元に何も無いと generate.py が動かず、
    見た目の確認が dev2 頼みになって、公開直前まで問題に気づけない。
    公開手順は dev(テストデータ) → dev2(実データ) → Cloudflare の 3 段で、
    その 1 段目を成り立たせるのがこのスクリプト。

置き場所
    store/ master/ data/ public_amedas/ に直接置く。いずれも .gitignore に
    あるので repo は汚れないし、generate.py を 1 行も変えずに済む。
    dev2 では同じ場所に実データが入る——コードから見れば同じ形。

作り物であることを隠さない
    地点数を絞り（既定 40）、地点名の先頭に印は付けないが、_meta.source を
    "TESTDATA" にする。generate.py が出す HTML を見れば分かるようにしておく。
    実データを上書きしないよう、store/ に実物がある場合は --force が要る。

使い方
    python make_testdata.py            # 作る（既存があれば止まる）
    python make_testdata.py --force    # 作り直す
    python make_testdata.py --stations 80 --days 40
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
import sqlite3
import warnings
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weatherlib.stations import MAIN_STATIONS

BASE = Path(__file__).resolve().parent
STORE = BASE / "store"
MASTER = BASE / "master"
DATA = BASE / "data"
POINT = BASE / "public_amedas" / "point"
EXTRA = BASE / "public_amedas" / "extra"
ADOC = BASE / "stations" / "amedastable.adoc"


"""主要都市はトップページの表と地点切替チップが参照するので必ず入れる。
名簿は weatherlib.stations.MAIN_STATIONS が正本（57 件）。ここに手で書き写すと
本番とずれるし、国際地点番号を直書きするとアメダス改番で黙って落ちる。"""


def load_stations() -> dict:
    """git 管理の観測所表を種にする。地点名も座標も本物なので、
    レイアウト確認には実データと同じ振る舞いになる。"""
    sys.path.insert(0, str(BASE))
    from weatherlib.station_adoc import load_adoc
    return load_adoc(ADOC)


def pick(table: dict, n: int) -> list[str]:
    """全国に散らばるように、アメダス番号順から等間隔で拾う。"""
    keys = sorted(table)
    step = max(1, len(keys) // n)
    chosen = keys[::step][:n]
    return sorted(set(chosen))


def seasonal(doy: int, lat: float, amp: float = 105.0) -> float:
    """緯度と季節から気温らしい値（0.1degC 単位）を作る。
    北ほど寒く、8 月が暑い。数値そのものに意味は無いが、
    ランキングや色分けが動くだけの散らばりは持たせる。"""
    base = 380 - (lat - 26.0) * 17.0
    return base + amp * math.sin((doy - 115) / 365.0 * 2 * math.pi)


def elements_of(elems: str) -> dict:
    """adoc の elems 文字列を stations.json の真偽値 4 つに直す。
    位置の定義は build_master.py が実測で特定したものをそのまま使う
    （ここで独自に定義すると本番と食い違う）。"""
    from build_master import ELEM_TEMP, ELEM_PRECIP, ELEM_SNOW, ELEM_SUN
    at = lambda i: elems[i:i + 1] == "1"
    return {"temp": at(ELEM_TEMP), "precip": at(ELEM_PRECIP),
            "snow": at(ELEM_SNOW), "sun": at(ELEM_SUN)}


def resolve_must_have(table: dict) -> tuple[list[str], dict[str, int]]:
    """主要都市のアメダス番号と、その国際地点番号を返す。
    国際地点番号を実物に合わせないと、トップページの主要都市表が空になる
    （generate.py は MAIN_STATIONS の code で引く）。"""
    by_name: dict[str, list[str]] = {}
    for amedas, r in table.items():
        by_name.setdefault(r["kjName"], []).append(amedas)
    order, intl = [], {}
    for s in MAIN_STATIONS:
        hits = by_name.get(s["name"], [])
        if not hits:
            print(f"  警告: 主要都市「{s['name']}」が観測所表にありません", file=sys.stderr)
            continue
        order.append(hits[0])
        intl[hits[0]] = s["code"]
    return order, intl


def build_master(table: dict, codes: list[str], main_intl: dict[str, int]) -> dict:
    stations, a2c = {}, {}
    for row, amedas in enumerate(codes):
        r = table[amedas]
        # 統計 code はアメダス番号と別系統。主要都市は実物の国際地点番号を使う
        # （generate.py が MAIN_STATIONS の code で引くため）。それ以外は
        # 衝突しない番号帯を振る。
        kansho = amedas in main_intl
        code = main_intl.get(amedas, 90000 + row)
        # load_adoc は lat/lon を [度, 分] の対で返す
        lat = r["lat"][0] + r["lat"][1] / 60.0
        lon = r["lon"][0] + r["lon"][1] / 60.0
        stations[str(code)] = {
            "amedas": amedas, "row": row, "name": r["kjName"], "kana": r["knName"],
            "en": r["enName"], "pref": "テスト地方",
            "intl": code if kansho else None,
            "lat": round(lat, 4), "lon": round(lon, 4), "alt": r["alt"],
            "type": r["type"], "elements": elements_of(r["elems"]),
            # 実データでは etrn が None の地点は 1 つも無い（1286/1286 が辞書）。
            # None にすると generate.py の climate_targets が落ちる。
            "etrn": {"prec_no": int(amedas[:2]), "block_no": str(code),
                     "type": "s" if kansho else "a"},
        }
        a2c[amedas] = code
    return {
        "_meta": {"built_at": datetime.now().isoformat(timespec="seconds"),
                  "source": "TESTDATA", "count": len(stations)},
        "index": {"amedas_to_code": a2c},
        "stations": stations,
    }


def write_nc(master: dict, days: int, rng: random.Random) -> list[date]:
    """観測 nc を作る。スキーマは NcStore に作らせる（手書きすると本番とずれる）。

    date / time の添字は 1870-01-01 と 2020-01-01 からの絶対値。generate.py の
    History.matrix が date_index() で引くので、0 から詰めると範囲外になる。
    実データと同じく、原点から今日までの疎な配列に、末尾 days 日分だけ値を置く。
    """
    from weatherlib.ncstore import NcStore, date_index, hour_index, FILL, FILL_B

    # netCDF4 1.7 が内部で配列の shape を書き換えるため numpy 2.5 の非推奨警告が
    # 出る。こちらの書き方では避けられないので、この関数の中だけ黙らせる。
    warnings.filterwarnings("ignore", category=DeprecationWarning,
                            message=".*Setting the shape on a NumPy array.*")

    st = master["stations"]
    codes = sorted(st, key=lambda c: st[c]["row"])
    today = date.today()
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]

    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "observations.nc").unlink(missing_ok=True)
    conn = sqlite3.connect(STORE / "weather.sqlite")
    store = NcStore(STORE / "observations.nc", conn)
    ds = store.ds
    ds.title = "TESTDATA - not real observations"

    for c in codes:                       # row を NcStore に割り当てさせる
        store.station_index(st[c]["amedas"])
    conn.commit()

    j0, j1 = date_index(dates[0]), date_index(dates[-1]) + 1
    h0 = hour_index(datetime.combine(dates[0], datetime.min.time()))
    h1 = h0 + days * 24
    # 座標変数は添字そのもの（date[i] = 原点からの日数）
    ds["date"][:j1] = np.arange(j1, dtype="i4")
    ds["time"][:h1] = np.arange(h1, dtype="i4")

    for c in codes:
        row = store.sidx[st[c]["amedas"]]
        lat = st[c]["lat"]
        hi = np.empty(days, "i2"); lo = np.empty(days, "i2"); av = np.empty(days, "i2")
        pr = np.empty(days, "i2"); su = np.empty(days, "i2")
        hourly = np.empty(days * 24, "i2")
        ph = np.zeros(days * 24, "i2"); sh = np.zeros(days * 24, "i2")
        for j, d in enumerate(dates):
            mid = seasonal(d.timetuple().tm_yday, lat) + rng.uniform(-35, 35)
            swing = rng.uniform(45, 95)
            hi[j] = int(mid + swing); lo[j] = int(mid - swing); av[j] = int(mid)
            wet = rng.random() < 0.30
            pr[j] = int(rng.uniform(5, 420)) if wet else 0
            su[j] = 0 if wet else int(rng.uniform(10, 110))
            for h in range(24):
                # 日中に山が来る形。最低は明け方、最高は 14 時ごろ
                hourly[j * 24 + h] = int(mid + swing * math.sin((h - 8) / 24 * 2 * math.pi))
            if wet:
                ph[j * 24 + rng.randrange(24)] = int(pr[j])
            else:
                for h in range(7, 16):
                    sh[j * 24 + h] = 10
        ds["temp"][row, h0:h1] = hourly
        ds["precip1h"][row, h0:h1] = ph
        ds["sun1h"][row, h0:h1] = sh
        ds["tmax"][row, j0:j1] = hi
        ds["tmin"][row, j0:j1] = lo
        ds["tavg"][row, j0:j1] = av
        ds["precip"][row, j0:j1] = pr
        ds["sun"][row, j0:j1] = su
        ds["tmax_minutes"][row, j0:j1] = 840
        ds["tmin_minutes"][row, j0:j1] = 300
        for q in ("tmax_q", "tmin_q", "tavg_q", "precip_q"):
            ds[q][row, j0:j1] = 8
        ds["tavg_count"][row, j0:j1] = 24
        ds["precip_none"][row, j0:j1] = 0

    store.close()
    conn.close()
    return dates


def write_sqlite_schema() -> None:
    """空の帳簿を作る。row の割り当ては NcStore.station_index に任せるので、
    stations は空のまま渡す（実運用と同じ順序: nc が row を決め、
    build_master が code を後から埋める）。"""
    STORE.mkdir(parents=True, exist_ok=True)
    p = STORE / "weather.sqlite"
    p.unlink(missing_ok=True)
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE stations (
            row INTEGER PRIMARY KEY, code INTEGER UNIQUE, amedas TEXT,
            intl TEXT, pref TEXT, name TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE amedas_log (
            row INTEGER NOT NULL, amedas TEXT NOT NULL,
            valid_from TEXT, valid_to TEXT);
        CREATE TABLE ingest_log (
            kind TEXT NOT NULL, key TEXT NOT NULL, fetched_at TEXT NOT NULL,
            PRIMARY KEY (kind, key));
        CREATE TABLE correction_log (
            logged_at TEXT NOT NULL, amedas TEXT NOT NULL, date TEXT NOT NULL,
            field TEXT NOT NULL, old_value INTEGER, new_value INTEGER);
        CREATE TABLE votes_raw (
            ip_hash TEXT NOT NULL, date TEXT NOT NULL, code INTEGER NOT NULL,
            vote INTEGER NOT NULL, voted_at TEXT);
        CREATE TABLE station_change (
            seen_at TEXT NOT NULL, amedas TEXT NOT NULL, kind TEXT NOT NULL,
            field TEXT, name TEXT, old_value TEXT, new_value TEXT);
    """)
    c.commit(); c.close()


def fill_station_meta(master: dict) -> None:
    """NcStore が割り当てた row に、code や名前を後から埋める（build_master の役）。
    row がずれていたら黙って進めず止める。"""
    now = datetime.now().isoformat(timespec="minutes")
    c = sqlite3.connect(STORE / "weather.sqlite")
    by_amedas = {s["amedas"]: (code, s) for code, s in master["stations"].items()}
    for row, amedas in c.execute("SELECT row, amedas FROM stations").fetchall():
        code, s = by_amedas[amedas]
        if row != s["row"]:
            raise SystemExit(
                f"row がずれました: {amedas} は nc で {row}、master で {s['row']}")
        c.execute("UPDATE stations SET code=?, intl=?, pref=?, name=?,"
                  " first_seen=?, last_seen=? WHERE row=?",
                  (int(code), s["intl"], s["pref"], s["name"], now, now, row))
    c.commit(); c.close()


def write_normals(master: dict) -> None:
    """平年値。雨温図と平年差の表示に要る。"""
    out = MASTER / "normals"
    out.mkdir(parents=True, exist_ok=True)
    for code, s in master["stations"].items():
        lat = s["lat"]
        daily, monthly = {}, {"tavg": [], "tmax": [], "tmin": [], "precip": [], "sun": []}
        for m in range(1, 13):
            ndays = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
            doy0 = sum([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30][:m - 1])
            av = [int(seasonal(doy0 + d, lat)) for d in range(1, ndays + 1)]
            daily[str(m)] = {
                "tavg": av,
                "tmax": [v + 55 for v in av],
                "tmin": [v - 55 for v in av],
                "precip": [int(80 + 40 * math.sin(m / 12 * 2 * math.pi))] * ndays,
            }
            monthly["tavg"].append(int(sum(av) / len(av)))
            monthly["tmax"].append(int(sum(av) / len(av)) + 55)
            monthly["tmin"].append(int(sum(av) / len(av)) - 55)
            monthly["precip"].append(int(1200 + 600 * math.sin(m / 12 * 2 * math.pi)))
            monthly["sun"].append(int(1500 + 300 * math.cos(m / 12 * 2 * math.pi)))
        (out / f"{code}.json").write_text(json.dumps({
            "code": int(code), "amedas": s["amedas"], "name": s["name"],
            "daily": daily, "monthly": monthly}, ensure_ascii=False), encoding="utf-8")


WCODES = [("晴れ", "100"), ("くもり", "200"), ("雨", "300"), ("雪", "400")]


def weather_for(temp_x10: float, rng: random.Random) -> tuple[str, str]:
    """気温に見合った天気を選ぶ。8 月の大阪が雪になると見た目の確認の邪魔になる。"""
    pool = WCODES if temp_x10 < 30 else WCODES[:3]
    return rng.choice(pool)


def write_data(master: dict, dates: list[date], rng: random.Random) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    st = master["stations"]
    now = datetime.now().replace(second=0, microsecond=0)
    slot = now.replace(minute=now.minute // 10 * 10)
    today = dates[-1]

    cur = {}
    for code, s in st.items():
        t = int(seasonal(today.timetuple().tm_yday, s["lat"]))
        w, wc = weather_for(t, rng)
        cur[code] = {"temp": t, "wthr": w, "wcode": wc}
    (DATA / "current.json").write_text(json.dumps({
        "amedas_time": slot.strftime("%Y-%m-%dT%H:%M"),
        "wthr_time": now.strftime("%Y-%m-%dT%H:00"),
        "stations": cur}, ensure_ascii=False), encoding="utf-8")

    fc = {}
    for code, s in st.items():
        days = {}
        for k in range(3):
            d = today + timedelta(days=k)
            base = seasonal(d.timetuple().tm_yday, s["lat"]) / 10
            w, wc = weather_for(base * 10, rng)
            days[d.isoformat()] = {"weather": w, "wcode": wc,
                                   "tmax": int(base + 5), "tmin": int(base - 5)}
        fc[code] = days
    (DATA / "forecast.json").write_text(json.dumps({
        "reported": now.strftime("%Y-%m-%dT%H:00"), "target_date": today.isoformat(),
        "target_label": "today", "stations": fc}, ensure_ascii=False), encoding="utf-8")

    hot = sum(1 for c in cur.values() if c["temp"] >= 350)
    (DATA / "today_meta.json").write_text(json.dumps({
        "source_time": now.strftime("%Y-%m-%dT%H:00"),
        "counts": {"moushobi": hot, "manatsubi": len(cur) // 2, "natsubi": len(cur),
                   "mafuyubi": 0, "fuyubi": 0, "nettaiya": len(cur) // 3}},
        ensure_ascii=False), encoding="utf-8")

    cols = ["code", "amedas", "tmax", "tmax_at", "tmax_q", "tmin", "tmin_at", "tmin_q",
            "year_tmax", "year_tmax_date", "year_tmin", "year_tmin_date",
            "record_tmax", "record_tmax_date", "month_tmax", "month_tmax_date",
            "record_tmin", "record_tmin_date", "month_tmin", "month_tmin_date"]
    with (DATA / "today.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for code, s in st.items():
            mid = seasonal(today.timetuple().tm_yday, s["lat"])
            hi, lo = int(mid + 70), int(mid - 70)
            w.writerow([code, s["amedas"], hi, "14:04", 8, lo, "05:12", 8,
                        hi + 30, today.isoformat(), lo - 30, today.isoformat(),
                        hi + 90, "1994-08-07", hi + 60, "2018-08-22",
                        lo - 140, "1984-02-17", lo - 80, "1996-01-25"])


def write_mirror(master: dict, rng: random.Random, slots: int = 8) -> None:
    """アメダスの 10 分値を**地点別・エリア束**の形で置く。

    本番と同じ形にしておかないとテストにならない。Worker が R2 に置き、
    dev2 が取り寄せたあとの姿を再現する:

        public_amedas/point/{YYYYMMDD}/{HH}/{area_code}.json
        中身は {アメダス番号: {時刻(14桁): {要素: [値, 品質]}}}

    エリアは master/area_map.json が正だが、テストでは作らないので
    アメダス番号の上 2 桁（府県予報区）で束ねる。束ね方が本番と違っても、
    読む側は「その日のエリア束を全部見る」ので結果は変わらない。
    """
    for d in (POINT, EXTRA):
        if d.exists():
            shutil.rmtree(d)
    st = master["stations"]
    now = datetime.now().replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute // 10 * 10)

    # {日付: {ブロック: {エリア: {番号: {時刻: 値}}}}}
    tree: dict[str, dict[str, dict[str, dict[str, dict]]]] = {}
    for k in range(slots):
        t = now - timedelta(minutes=10 * (slots - 1 - k))
        day, block, key = f"{t:%Y%m%d}", f"{t.hour // 3 * 3:02d}", f"{t:%Y%m%d%H%M}00"
        for s in st.values():
            temp = seasonal(t.timetuple().tm_yday, s["lat"]) / 10 + rng.uniform(-2, 2)
            area = s["amedas"][:2]
            (tree.setdefault(day, {}).setdefault(block, {})
                 .setdefault(area, {}).setdefault(s["amedas"], {}))[key] = {
                # 対でない素の整数。pointstore が除くことの確認も兼ねる
                "prefNumber": int(area), "observationNumber": int(s["amedas"][2:]),
                "temp": [round(temp, 1), 0],
                "humidity": [rng.randrange(35, 95), 0],
                "pressure": [round(rng.uniform(995, 1020), 1), 0],
                "normalPressure": [round(rng.uniform(1000, 1025), 1), 0],
                "precipitation10m": [round(max(0.0, rng.gauss(0, 0.6)), 1), 0],
                "precipitation1h": [round(max(0.0, rng.gauss(0, 2.0)), 1), 0],
                "wind": [round(rng.uniform(0, 12), 1), 0],
                "windDirection": [rng.randrange(0, 17), 0],
                "gust": [round(rng.uniform(2, 25), 1), 0],
                "sun10m": [rng.randrange(0, 11), 0],
                "maxTemp": [round(temp + rng.uniform(0, 4), 1), 0],
                "minTemp": [round(temp - rng.uniform(0, 4), 1), 0],
            }

    # map 由来の補完分。地点別に無い要素（積雪・天気）はこちらから来る。
    # weather は正時のスロットにだけ入れる（実測 2026-08-29: 正時 150 地点 /
    # 非正時 0 地点）。欠測＝キーごと無い、を再現しておく。
    n_extra = 0
    for k in range(slots):
        t = now - timedelta(minutes=10 * (slots - 1 - k))
        key = f"{t:%Y%m%d%H%M}"
        ex = {}
        for s_ in st.values():
            vals = {}
            if t.minute == 0:
                vals["weather"] = [rng.choice([100, 200, 300]), 0]
            # 積雪は冬だけ。夏は要素ごと来ない
            if t.month in (12, 1, 2, 3) and s_["lat"] > 36:
                vals["snow"] = [rng.randrange(0, 80), 0]
                vals["snow24h"] = [rng.randrange(0, 30), 0]
            if vals:
                ex[s_["amedas"]] = vals
        if ex:
            d = EXTRA / key[:8]
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{key}.json").write_text(json.dumps(ex, ensure_ascii=False),
                                           encoding="utf-8")
            n_extra += 1

    n = 0
    for day, blocks in tree.items():
        for block, areas in blocks.items():
            out = POINT / day / block
            out.mkdir(parents=True, exist_ok=True)
            for area, stations in areas.items():
                (out / f"{area}.json").write_text(
                    json.dumps(stations, ensure_ascii=False), encoding="utf-8")
                n += 1
    return n, n_extra


def write_area_map(master: dict) -> int:
    """エリア対応表。fetch_points.py が「どのエリアに何地点あるか」を引く。

    本番は build_area_map.py が振興局と予報区から作るが、テストでは
    アメダス番号の上 2 桁（府県予報区）で束ねる。束ね方が本番と違っても、
    1 エリアの地点数が上限内かどうかを試すには足りる。
    """
    out = {}
    for s_ in master["stations"].values():
        area = s_["amedas"][:2]
        out[s_["amedas"]] = {"area": area, "pref": s_["pref"], "name": s_["name"],
                             "lat": s_["lat"], "lon": s_["lon"], "alt": s_["alt"],
                             "type": s_["type"]}
    MASTER.mkdir(parents=True, exist_ok=True)
    (MASTER / "area_map.json").write_text(json.dumps(
        {"note": "TESTDATA", "source": "TESTDATA", "count": len(out), "stations": out},
        ensure_ascii=False), encoding="utf-8")
    return len({v["area"] for v in out.values()})


def looks_real() -> bool:
    """実データを間違って潰さないための見張り。作り物には TESTDATA の印がある。"""
    p = MASTER / "stations.json"
    if not p.is_file():
        return (STORE / "observations.nc").is_file()
    try:
        return json.loads(p.read_text(encoding="utf-8"))["_meta"].get("source") != "TESTDATA"
    except Exception:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description="手元用のテストデータを作る")
    ap.add_argument("--stations", type=int, default=40, help="地点数（既定 40）")
    ap.add_argument("--days", type=int, default=30, help="日数（既定 30）")
    ap.add_argument("--seed", type=int, default=20260828, help="乱数の種")
    ap.add_argument("--force", action="store_true", help="既存を作り直す")
    args = ap.parse_args()

    if looks_real() and not args.force:
        print("store/ か master/ に TESTDATA でないものがあります。", file=sys.stderr)
        print("実データを潰さないため中断します。作り直すなら --force。", file=sys.stderr)
        return 1

    if not ADOC.is_file():
        print(f"観測所表がありません: {ADOC}", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    table = load_stations()
    must, main_intl = resolve_must_have(table)
    codes = list(must)
    codes += [a for a in pick(table, args.stations) if a not in codes]
    codes = codes[:max(args.stations, len(must))]

    master = build_master(table, codes, main_intl)
    MASTER.mkdir(parents=True, exist_ok=True)
    (MASTER / "stations.json").write_text(
        json.dumps(master, ensure_ascii=False), encoding="utf-8")

    write_sqlite_schema()
    dates = write_nc(master, args.days, rng)
    fill_station_meta(master)
    write_normals(master)
    write_data(master, dates, rng)
    n_area, n_extra = write_mirror(master, rng)
    n_groups = write_area_map(master)

    nc = (STORE / "observations.nc").stat().st_size
    print(f"テストデータを作りました（作り物です。実データではありません）")
    print(f"  地点 {len(codes)} / 日数 {args.days}（{dates[0]} 〜 {dates[-1]}）")
    print(f"  store/observations.nc  {nc:,} bytes")
    print(f"  master/normals/        {len(codes)} 件")
    print(f"  public_amedas/point/   {n_area} エリア束（{n_groups} エリア）")
    print(f"  master/area_map.json   {n_groups} エリア")
    print(f"  public_amedas/extra/   {n_extra} スロット（map 由来の補完分）")
    # 実行中の python をそのまま案内する（venv を決め打ちしない）
    print(f"\n次: {sys.executable} build_site.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
