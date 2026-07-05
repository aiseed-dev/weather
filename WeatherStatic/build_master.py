#!/usr/bin/env python3
"""マスターデータ構築（初回・平年値改訂時のみ実行）。

現在の対象:
  master/stations.json  … 全アメダス地点マスタ（DATA_CONTRACT 2.1）

ソース:
  1. アメダス地点表（公式 const）: 名前・カナ・英名・座標・標高・観測要素・種別
       https://www.jma.go.jp/bosai/amedas/const/amedastable.json
  2. store/weather.sqlite の stations（mdrr CSV 由来）: 国際地点番号・都道府県
  3. weatherlib/stations.py（C# 移植）: 主要 57 都市の PLACE・予報区域・office
  4. 平年値一括 ZIP（任意。あれば has_normals を正確化）

構築時に整合チェックを行い、不整合は警告として表示する。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weatherlib import jma
from weatherlib.stations import MAIN_STATIONS

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
SQLITE = BASE / "store" / "weather.sqlite"

URL_AMEDASTABLE = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
URL_ETRN_PREFS = "https://www.data.jma.go.jp/stats/etrn/select/prefecture00.php"
URL_ETRN_PREF = ("https://www.data.jma.go.jp/stats/etrn/select/prefecture.php"
                 "?prec_no={prec}&block_no=&year=&month=&day=&view=")
ETRN_INTERVAL = 1.0

# amedastable の elems フラグ位置（map JSON との突合で実測特定・2026-07-05）
# [0]=気温 [1]=降水量 [2,3]=風 [4]=気圧(官署) [5]=積雪 [6]=日照 [7]=海面気圧(官署)
ELEM_TEMP, ELEM_PRECIP, ELEM_SNOW, ELEM_SUN = 0, 1, 5, 6


def log(msg: str) -> None:
    print(f"[master] {msg}", flush=True)


def fetch_etrn_index() -> dict[tuple, dict]:
    """etrn（統計ページ）の地点一覧を、座標 (度, 分) キーの辞書で返す。

    master/station_codes.json（fetch_station_codes.py の出力）があればそれを使い、
    無ければ weatherlib.etrn で取得する。
    アメダス番号と etrn の block_no は**別体系**（例: 八王子 = アメダス 44116 / etrn 0366）。
    """
    from weatherlib import etrn as etrn_mod
    codes_path = MASTER / "station_codes.json"
    if codes_path.exists():
        obj = json.loads(codes_path.read_text(encoding="utf-8"))
        entries = obj["entries"]
        log(f"etrn 地点一覧: {codes_path.name} を使用（{obj['_meta']['fetched_at']} 取得・"
            f"{len(entries)} 地点。更新は fetch_station_codes.py）")
    else:
        entries = etrn_mod.fetch_all(log=log, interval=ETRN_INTERVAL)

    index: dict[tuple, dict] = {}
    for e in entries:
        key = (e["lat_dm"][0], round(e["lat_dm"][1], 1),
               e["lon_dm"][0], round(e["lon_dm"][1], 1))
        # 同一座標に複数（廃止→新設の引き継ぎ等）は現役を優先
        if key not in index or (e["active"] and not index[key]["active"]):
            index[key] = e
    return index


def diff_previous(stations: dict) -> None:
    """前回の stations.json と比較し、アメダス番号の増減・改番候補を報告する。"""
    prev_path = MASTER / "stations.json"
    if not prev_path.exists():
        return
    prev = json.loads(prev_path.read_text(encoding="utf-8"))["stations"]
    # 旧形式（アメダス番号キー）にも対応
    prev_by_amedas = {r.get("amedas", k): r for k, r in prev.items()}
    cur_by_amedas = {r["amedas"]: r for r in stations.values()}
    added = set(cur_by_amedas) - set(prev_by_amedas)
    removed = set(prev_by_amedas) - set(cur_by_amedas)
    if not added and not removed:
        log("  前回ビルドとの差分: なし")
        return
    for a in sorted(added):
        r = cur_by_amedas[a]
        # 改番候補: 消えたアメダス番号と座標が近いもの → --renumber で sid を引き継げる
        cand = [b for b in removed
                if abs(prev_by_amedas[b]["lat"] - r["lat"]) < 0.02
                and abs(prev_by_amedas[b]["lon"] - r["lon"]) < 0.02]
        log(f"  ＋新規 {a} {r['name']}"
            + (f" ← 改番候補: {cand}（build_master.py --renumber {cand[0]} {a} で code・系列を引継可）" if cand else ""))
    for b in sorted(removed):
        log(f"  −消滅 {b} {prev_by_amedas[b]['name']}")


def renumber(old: str, new: str) -> int:
    """アメダス番号の改番を code・row を維持したまま適用する。

    観測系列（observations.nc の行）は row で同一性が保たれるため、
    改番後も同じ行に蓄積が継続する。番号履歴は amedas_log に残す。
    """
    import netCDF4 as nc
    conn = sqlite3.connect(SQLITE)
    hit = conn.execute("SELECT row, code, name FROM stations WHERE amedas = ?", (old,)).fetchone()
    if hit is None:
        log(f"✗ 旧番号 {old} が見つかりません")
        return 1
    row, code, name = hit
    if conn.execute("SELECT 1 FROM stations WHERE amedas = ?", (new,)).fetchone():
        log(f"✗ 新番号 {new} は既に別地点に使われています（改番ではなく別地点の可能性）")
        return 1
    now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    conn.execute("UPDATE amedas_log SET valid_to = ? "
                 "WHERE row = ? AND valid_to IS NULL", (now, row))
    conn.execute("INSERT INTO amedas_log (row, amedas, valid_from, valid_to) "
                 "VALUES (?, ?, ?, NULL)", (row, new, now))
    conn.execute("UPDATE stations SET amedas = ? WHERE row = ?", (new, row))
    conn.commit()
    ncp = BASE / "store" / "observations.nc"
    if ncp.exists():
        ds = nc.Dataset(ncp, "a")
        ds["station_id"][row] = new
        ds.close()
    log(f"改番を適用: {name} (code={code}, row={row}) {old} → {new}。"
        "code・観測系列は不変。stations.json を再ビルドしてください")
    return 0


def build_stations() -> dict:
    log("アメダス地点表を取得中...")
    amd = json.loads(jma.http_get(URL_AMEDASTABLE))
    log(f"  {len(amd)} 地点")

    conn = sqlite3.connect(SQLITE)
    sq = {r[0]: {"row": r[1], "code": r[2], "intl": r[3], "pref": r[4]}
          for r in conn.execute("SELECT amedas, row, code, intl, pref FROM stations")}

    # 都道府県: mdrr CSV 由来（気温観測地点のみ判明）。残りはエリア番号(先頭2桁)の多数決で補完。
    # 北海道のエリア 15(上川/空知混在)・24(渡島/檜山混在)は多数決になる点に注意。
    area_votes: dict[str, Counter] = {}
    for a, v in sq.items():
        if v["pref"]:
            area_votes.setdefault(a[:2], Counter())[v["pref"]] += 1
    area2pref = {area: c.most_common(1)[0][0] for area, c in area_votes.items()}

    # 平年値の有無
    nml: set[str] = set()
    for zname in ("normal_amedas_daily.zip",):
        for p in (MASTER / "raw" / zname,
                  Path("/tmp") / zname):
            if p.exists():
                z = zipfile.ZipFile(p)
                nml = {m.group(1) for n in z.namelist()
                       if (m := re.search(r"nml_amd_d_(\d+)\.csv", n))}
                break
    if not nml:
        log("  注意: 平年値 ZIP が見つからないため has_normals は省略します"
            "（master/raw/normal_amedas_daily.zip に置くと正確化）")

    # 主要 57 都市: 国際地点番号 → アメダス番号
    intl2amedas = {v["intl"]: a for a, v in sq.items() if v["intl"]}
    main_by_amedas = {}
    missing = []
    for s in MAIN_STATIONS:
        a = intl2amedas.get(s["code"])
        if a is None:
            missing.append(s["name"])
            continue
        main_by_amedas[a] = s
    if missing:
        log(f"  ✗ 主要都市がアメダス番号に解決できません: {missing}")

    # 統計（etrn）ページの地点番号。アメダス番号とは別体系のため座標で突合する
    etrn = fetch_etrn_index()

    def match_etrn(e, name):
        key = (e["lat"][0], round(e["lat"][1], 1), e["lon"][0], round(e["lon"][1], 1))
        hit = etrn.get(key)
        if hit:
            return hit
        # 座標が微妙にずれている場合: ±0.3 分で走査し、名前一致を優先
        near = [v for (lad, lam, lod, lom), v in etrn.items()
                if lad == key[0] and lod == key[2]
                and abs(lam - key[1]) <= 0.3 and abs(lom - key[3]) <= 0.3]
        for v in near:
            if v["name"] == name:
                return v
        return near[0] if len(near) == 1 else None

    # 主キーは code（国際地点番号、無ければ etrn の 4 桁地点番号）。
    # アメダス番号は改番されうるため属性として持つ。row は nc の行番号（内部）
    stations = {}
    n_pref_fallback = 0
    n_etrn = 0
    etrn_unmatched = []
    no_row = []
    code_updates = []
    for a, e in sorted(amd.items()):
        row = sq.get(a, {}).get("row")
        if row is None:
            no_row.append((a, e["kjName"]))   # 蓄積で未観測（accumulate 実行後に再ビルド）
            continue
        lat = round(e["lat"][0] + e["lat"][1] / 60, 4)
        lon = round(e["lon"][0] + e["lon"][1] / 60, 4)
        elems = e.get("elems", "")
        pref = sq.get(a, {}).get("pref")
        if not pref:
            pref = area2pref.get(a[:2])
            n_pref_fallback += 1
        rec = {
            "amedas": a,                       # 現在のアメダス番号（属性）
            "row": row,                        # observations.nc の行番号（内部・不変）
            "name": e["kjName"], "kana": e.get("knName"), "en": e.get("enName"),
            "pref": pref,
            "intl": sq.get(a, {}).get("intl"),
            "lat": lat, "lon": lon, "alt": e.get("alt"),
            "type": e.get("type"),
            "elements": {
                "temp": elems[ELEM_TEMP:ELEM_TEMP + 1] == "1",
                "precip": elems[ELEM_PRECIP:ELEM_PRECIP + 1] == "1",
                "snow": elems[ELEM_SNOW:ELEM_SNOW + 1] == "1",
                "sun": elems[ELEM_SUN:ELEM_SUN + 1] == "1",
            },
        }
        if nml:
            rec["has_normals"] = a in nml
        hit = match_etrn(e, e["kjName"])
        if hit:
            rec["etrn"] = {"prec_no": hit["prec_no"], "block_no": hit["block_no"],
                           "type": hit["type"]}
            n_etrn += 1
            # 官署なのに mdrr に現れない特殊地点（南鳥島・富士山）の国際地点番号を補完
            if rec["intl"] is None and hit["type"] == "s":
                rec["intl"] = int(hit["block_no"])
        else:
            etrn_unmatched.append((a, e["kjName"]))
        m = main_by_amedas.get(a)
        if m:
            rec.update({"main": True, "place": m["place"], "area": m["area_str"],
                        "office": m["office"], "pref_code": m["pref_code"]})

        # 地点番号（主キー）を決定: 国際地点番号 → etrn 4 桁番号
        code = rec["intl"] or (int(rec["etrn"]["block_no"]) if "etrn" in rec else None)
        if code is None:
            etrn_unmatched.append((a, e["kjName"] + "（code 未解決）"))
            continue
        old_code = sq.get(a, {}).get("code")
        if old_code is None:
            code_updates.append((code, a))
        elif old_code != code:
            log(f"  ✗ code が変化: {e['kjName']} {old_code} → {code}（要確認。sqlite は更新しません）")
        if str(code) in stations:
            log(f"  ✗ code 重複: {code}（{stations[str(code)]['name']} と {e['kjName']}）")
            continue
        stations[str(code)] = rec
    if code_updates:
        conn.executemany("UPDATE stations SET code = ? WHERE amedas = ? AND code IS NULL",
                         code_updates)
        conn.commit()
        log(f"  code を sqlite に反映: {len(code_updates)} 地点")
    if no_row:
        log(f"  ✗ row 未割当（accumulate 未実行?）: {len(no_row)} 地点 {no_row[:5]}")

    # ---- チェック ----
    log("整合チェック:")
    n_temp = sum(1 for r in stations.values() if r["elements"]["temp"])
    n_main = sum(1 for r in stations.values() if r.get("main"))
    n_intl = sum(1 for r in stations.values() if r["intl"])
    log(f"  地点 {len(stations)} / 気温観測 {n_temp} / 官署(国際番号) {n_intl} / 主要都市 {n_main}")
    if n_main != len(MAIN_STATIONS):
        log(f"  ✗ 主要都市が {n_main}/{len(MAIN_STATIONS)} しか解決していません")
    no_pref = [a for a, r in stations.items() if not r["pref"]]
    if no_pref:
        log(f"  ✗ 都道府県が不明: {no_pref[:10]}")
    log(f"  都道府県をエリア多数決で補完: {n_pref_fallback} 地点（気温観測なし地点）")
    if nml:
        no_nml = [(a, r["name"]) for a, r in stations.items()
                  if r["elements"]["temp"] and not r.get("has_normals")]
        log(f"  気温観測ありで平年値なし（新設等）: {len(no_nml)} 地点 {no_nml[:10]}")
    log(f"  etrn（統計）地点番号の突合: {n_etrn}/{len(stations)}")
    if etrn_unmatched:
        log(f"  ✗ etrn 未突合: {len(etrn_unmatched)} 地点 {etrn_unmatched[:10]}")
    # 官署の整合: etrn の官署 block_no は国際地点番号と一致するはず
    bad_intl = [(a, r["name"], r["intl"], r["etrn"]["block_no"])
                for a, r in stations.items()
                if r["intl"] and r.get("etrn", {}).get("type") == "s"
                and str(r["intl"]) != r["etrn"]["block_no"]]
    if bad_intl:
        log(f"  ✗ 官署番号の不一致: {bad_intl[:5]}")
    else:
        log("  官署の国際地点番号 = etrn block_no: 一致 ✓")

    diff_previous(stations)

    return {
        "_meta": {
            "built_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
            "source": URL_AMEDASTABLE,
            "station_count": len(stations),
        },
        "index": {  # 逆引き（現在のアメダス番号 → code）
            "amedas_to_code": {r["amedas"]: int(code) for code, r in stations.items()},
        },
        "stations": stations,
    }


# ---------------------------------------------------------------- 平年値マスター

# 要素コード（官署・アメダス、日別・月別とも共通。東京で etrn 表示値と突合済み）
NML_ELEMS = {"0500": "tavg", "0600": "tmax", "0700": "tmin",
             "4000": "precip", "3500": "sun"}
NML_MIN_QUALITY = 5   # 品質フラグがこれ未満の値は null 扱い（旧サイトの 注>=5 と同じ）


def _parse_nml_daily(raw: str) -> dict:
    """日別平年値 CSV → {"月": {要素: [日別値...]}}"""
    out: dict[str, dict] = {}
    for line in raw.splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) < 9 or c[2] not in NML_ELEMS:
            continue
        month, elem = c[6], NML_ELEMS[c[2]]
        vals = []
        for i in range(7, len(c) - 1, 2):
            v, q = c[i], c[i + 1]
            try:
                vals.append(int(v) if int(q or 0) >= NML_MIN_QUALITY else None)
            except ValueError:
                vals.append(None)
        out.setdefault(month, {})[elem] = vals
    return out


def _parse_nml_monthly(raw: str) -> dict:
    """月別平年値 CSV → {要素: [12 か月], "year": {要素: 年間値}}"""
    months: dict[str, list] = {}
    year: dict[str, int | None] = {}
    for line in raw.splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) < 32 or c[2] not in NML_ELEMS:
            continue
        elem = NML_ELEMS[c[2]]
        vals = []
        for i in range(6, 6 + 24, 2):          # 12 か月分
            v, q = c[i], c[i + 1]
            try:
                vals.append(int(v) if int(q or 0) >= NML_MIN_QUALITY else None)
            except ValueError:
                vals.append(None)
        months[elem] = vals
        try:
            year[elem] = int(c[30]) if int(c[31] or 0) >= NML_MIN_QUALITY else None
        except (ValueError, IndexError):
            year[elem] = None
    months["year"] = year
    return months


def build_normals() -> int:
    """平年値一括 ZIP → master/normals/{code}.json（現役全地点）。"""
    import zipfile as _zf
    st_path = MASTER / "stations.json"
    if not st_path.exists():
        log("✗ master/stations.json がありません（先に build_master.py を実行）")
        return 1
    stations = json.loads(st_path.read_text(encoding="utf-8"))["stations"]

    raw_dir = MASTER / "raw"
    z_sfc = _zf.ZipFile(raw_dir / "normal_surface.zip")
    z_amd_d = _zf.ZipFile(raw_dir / "normal_amedas_daily.zip")
    z_amd_m = _zf.ZipFile(raw_dir / "normal_amedas_monthly.zip")
    amd_path = {}   # (種別, アメダス番号) → zip 内パス（area ディレクトリを吸収）
    for z, key in ((z_amd_d, "d"), (z_amd_m, "m")):
        for n in z.namelist():
            m = re.search(r"nml_amd_([dm])_(\d+)\.csv$", n)
            if m:
                amd_path[(m.group(1), m.group(2))] = n

    out_dir = MASTER / "normals"
    out_dir.mkdir(parents=True, exist_ok=True)
    n_sfc = n_amd = n_skip = 0
    for code, rec in stations.items():
        try:
            if rec["intl"]:   # 官署 → surface ZIP（種別 15/11）
                d_raw = z_sfc.read(f"normal_surface/daily/nml_sfc_d_{rec['intl']}.csv")
                m_raw = z_sfc.read(f"normal_surface/monthly/nml_sfc_m_{rec['intl']}.csv")
                n_sfc += 1
            else:             # アメダス → amedas ZIP（種別 25/21）
                d_raw = z_amd_d.read(amd_path[("d", rec["amedas"])])
                m_raw = z_amd_m.read(amd_path[("m", rec["amedas"])])
                n_amd += 1
        except KeyError:
            n_skip += 1       # 新設地点など平年値なし
            continue
        obj = {
            "code": int(code), "amedas": rec["amedas"], "name": rec["name"],
            "daily": _parse_nml_daily(d_raw.decode("cp932", errors="replace")),
            "monthly": _parse_nml_monthly(m_raw.decode("cp932", errors="replace")),
        }
        p = out_dir / f"{code}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    log(f"master/normals/ を出力: 官署 {n_sfc} + アメダス {n_amd} 地点"
        f"（平年値なしスキップ {n_skip}）")

    # 検証: 既知値（etrn 平年値ページの表示値）との突合
    tokyo = json.loads((out_dir / "47662.json").read_text(encoding="utf-8"))
    checks = [
        ("東京 7/1 最高", tokyo["daily"]["7"]["tmax"][0], 280),
        ("東京 7/1 降水", tokyo["daily"]["7"]["precip"][0], 58),
        ("東京 7/1 日照", tokyo["daily"]["7"]["sun"][0], 39),
        ("東京 1月 平均", tokyo["monthly"]["tavg"][0], 54),
        ("東京 年 降水", tokyo["monthly"]["year"]["precip"], 15982),
    ]
    ok = True
    for label, got, want in checks:
        mark = "✓" if got == want else f"✗ (期待 {want})"
        if got != want:
            ok = False
        log(f"  検証 {label}: {got} {mark}")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--renumber":
        return renumber(sys.argv[2], sys.argv[3])
    if len(sys.argv) == 2 and sys.argv[1] == "--normals":
        return build_normals()
    MASTER.mkdir(parents=True, exist_ok=True)
    obj = build_stations()
    out = MASTER / "stations.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    log(f"master/stations.json を出力（{obj['_meta']['station_count']} 地点・code キー）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
