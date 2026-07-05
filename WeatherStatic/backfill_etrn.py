#!/usr/bin/env python3
"""etrn（気象庁「過去の気象データ検索」）から不足期間の日別値を機械的に取得する。

旧バッチ（WeatherToolsCore）停止〜新蓄積開始の間のギャップを埋める用途と、
暫定値（map JSON 由来の日平均など）を確定値へ置き換える月次バッチの両方に使える。

  取得元:  官署   … stats/etrn/view/daily_s1.php?prec_no=&block_no=&year=&month=
           アメダス … stats/etrn/view/daily_a1.php（同形式）
           地点番号は master/stations.json の etrn フィールド（座標突合済み）を使用
  取得値:  平均気温・最高気温・最低気温・降水量（×10 整数化）＋品質
           （素値=8、` )`付き=準正常 5、` ]`付き=資料不足 4、`--`=現象なし）

機械的に回すための仕組み:
  - **自動スキップ**: nc に十分なデータがある地点×月は取得しない（--force で無効化）
  - **再開可能**: 取得済みの地点×月は ingest_log（etrn_daily）に記録し、次回はスキップ
  - **分割実行**: --limit N で 1 回あたりのページ数を制限（cron で夜間に少しずつ）
  - アクセスは 1 ページ/秒（気象庁への負荷抑制）

使い方:
    python backfill_etrn.py --from 2025-01 --to 2026-06            # 全気温観測地点
    python backfill_etrn.py --from 2025-01 --to 2026-06 --main-only
    python backfill_etrn.py --from 2025-01 --to 2026-06 --limit 500   # 夜間バッチ向け
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import warnings
from datetime import date, datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np

from weatherlib import jma
from weatherlib.ncstore import FILL, FILL_B, NcStore, date_index
from weatherlib.store import open_store

BASE = Path(__file__).resolve().parent
SQLITE = BASE / "store" / "weather.sqlite"
NC = BASE / "store" / "observations.nc"
MASTER = BASE / "master"

URL = ("https://www.data.jma.go.jp/stats/etrn/view/daily_{typ}1.php"
       "?prec_no={prec}&block_no={block}&year={y}&month={m}&day=&view=")
INTERVAL = 1.0

# 表の列位置（実ページで検証済み: 官署=東京 2026-07、アメダス=block 0365 2026-06）
#   s1: [日, 気圧現地, 気圧海面, 降水量合計, 最大1h, 最大10m, 平均気温, 最高, 最低, ...]
#   a1: [日, 降水量合計, 最大1h, 最大10m, 平均気温, 最高, 最低, ...]
COLS = {"s": {"precip": 3, "tavg": 6, "tmax": 7, "tmin": 8},
        "a": {"precip": 1, "tavg": 4, "tmax": 5, "tmin": 6}}


def log(msg: str) -> None:
    print(f"[etrn] {msg}", flush=True)


def parse_value(s: str) -> tuple[int | None, int | None, int]:
    """etrn セル → (×10 値, 品質, 降水無フラグ)。取得不能は (None, None, 0)。"""
    s = s.strip().replace("　", "")
    if s in ("", "///", "×", "#"):
        return None, None, 0
    if s == "--":                       # 現象なし（降水量 0 扱い）
        return 0, 8, 1
    q = 8
    if s.endswith(")"):
        q, s = 5, s[:-1].strip()        # 準正常値
    elif s.endswith("]"):
        q, s = 4, s[:-1].strip()        # 資料不足値
    try:
        return int(round(float(s) * 10)), q, 0
    except ValueError:
        return None, None, 0


def parse_page(html: str, typ: str) -> dict[int, dict]:
    """日別値ページ → {日: {tavg:(v,q), tmax:(v,q), tmin:(v,q), precip:(v,q,none)}}"""
    out: dict[int, dict] = {}
    cols = COLS[typ]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if not cells or not cells[0].isdigit():
            continue
        day = int(cells[0])
        rec = {}
        for field, idx in cols.items():
            if idx >= len(cells):
                continue
            v, q, none = parse_value(cells[idx])
            # 温度の妥当性チェック（列ずれの安全弁: -50〜+50℃）
            if field != "precip" and v is not None and not (-500 <= v <= 500):
                return {}
            rec[field] = (v, q, none)
        if rec:
            out[day] = rec
    return out


def month_range(from_s: str, to_s: str):
    y, m = map(int, from_s.split("-"))
    y2, m2 = map(int, to_s.split("-"))
    while (y, m) <= (y2, m2):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def days_in_month(y: int, m: int) -> int:
    return (date(y + (m == 12), m % 12 + 1, 1) - date(y, m, 1)).days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_", required=True, metavar="YYYY-MM")
    ap.add_argument("--to", dest="to", required=True, metavar="YYYY-MM")
    ap.add_argument("--main-only", action="store_true", help="主要 57 都市のみ")
    ap.add_argument("--limit", type=int, default=0, help="今回取得する最大ページ数")
    ap.add_argument("--force", action="store_true", help="既存カバレッジがあっても取得")
    args = ap.parse_args()

    stations = json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))["stations"]
    targets = [(int(c), r) for c, r in stations.items()
               if r["elements"]["temp"] and r.get("etrn")
               and (not args.main_only or r.get("main"))]
    targets.sort(key=lambda x: x[0])
    months = list(month_range(args.from_, args.to))
    log(f"対象: {len(targets)} 地点 × {len(months)} か月")

    conn = open_store(SQLITE)
    done = {k for (k,) in conn.execute(
        "SELECT key FROM ingest_log WHERE kind = 'etrn_daily'")}
    rowmap = dict(conn.execute("SELECT code, row FROM stations WHERE code IS NOT NULL"))

    work = NC.with_suffix(".nc.work")
    shutil.copy2(NC, work)
    ncs = NcStore(work, conn)
    ds = ncs.ds
    n_dates = ds.dimensions["date"].size

    def coverage(row: int, j0: int, nd: int) -> int:
        if j0 >= n_dates:
            return 0
        arr = ds["tmax"][row, j0:min(j0 + nd, n_dates)]
        return int((arr != FILL).sum())

    n_pages = n_cells = n_skip_cov = 0
    new_marks: list[str] = []
    today = datetime.now().date()
    stop = False
    try:
        for y, m in months:
            if stop:
                break
            if (y, m) >= (today.year, today.month):
                continue   # 進行中の月は対象外（確定後に月次で取る）
            j0 = date_index(date(y, m, 1))
            nd = days_in_month(y, m)
            for code, rec in targets:
                key = f"{code}-{y:04d}{m:02d}"
                if key in done:
                    continue
                row = rowmap.get(code)
                if row is None:
                    continue
                if not args.force and coverage(row, j0, nd) >= nd - 2:
                    new_marks.append(key)   # 既に充足 → 取得不要として記録
                    n_skip_cov += 1
                    continue
                e = rec["etrn"]
                url = URL.format(typ=e["type"], prec=e["prec_no"],
                                 block=e["block_no"], y=y, m=m)
                try:
                    html = jma.http_get(url).decode("utf-8", errors="replace")
                except Exception as ex:
                    log(f"  警告: {rec['name']} {y}-{m:02d} 取得失敗: {ex}")
                    continue
                days = parse_page(html, e["type"])
                if not days:
                    log(f"  警告: {rec['name']} {y}-{m:02d} 解析結果が空（列ずれ?）")
                else:
                    for day, fields in days.items():
                        dj = j0 + day - 1
                        for field, (v, q, none) in fields.items():
                            if v is None:
                                continue
                            ds[field][row, dj] = v                       # 確定値で上書き
                            ds[f"{field}_q"][row, dj] = q if q else FILL_B
                            if field == "precip" and none:
                                ds["precip_none"][row, dj] = 1
                            n_cells += 1
                    ds["date"][j0:j0 + nd] = np.arange(j0, j0 + nd, dtype=np.int32)
                new_marks.append(key)
                n_pages += 1
                if n_pages % 50 == 0:
                    log(f"  進捗: {n_pages} ページ / {n_cells:,} セル")
                if args.limit and n_pages >= args.limit:
                    log(f"--limit {args.limit} に到達。次回実行で続きから再開します")
                    stop = True
                    break
                time.sleep(INTERVAL)
    except KeyboardInterrupt:
        log("中断。ここまでの取得分を保存します（次回は続きから）")
    finally:
        ncs.close()

    work.replace(NC)
    # nc の保存が成功してから取得済みマークを記録（クラッシュしても取り直せる）
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany("INSERT OR REPLACE INTO ingest_log VALUES ('etrn_daily', ?, ?)",
                     [(k, now) for k in new_marks])
    conn.commit()
    conn.close()
    log(f"完了: {n_pages} ページ取得 / {n_cells:,} セル書込 / 充足スキップ {n_skip_cov}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
