#!/usr/bin/env python3
"""旧サイト PostgreSQL（weather DB）の jma_daily から観測履歴をバックフィルする。

WeatherToolsCore（旧データ取得バッチ）が蓄積した過去の 最高/最低/平均気温・降水量を
store/observations.nc に取り込む。**地点コードは code と同一体系・値は ×10 整数・
品質注は気象庁コード**なので変換は不要（キー変換なしでそのまま入る）。

入力（どちらでも可。Windows 側で作ってこの環境へコピーする）:
  1. pg_dump の COPY 形式:
       pg_dump -t jma_daily --data-only weather > jma_daily.dump
  2. CSV（ヘッダつき）:
       psql weather -c "\\copy jma_daily TO 'jma_daily.csv' CSV HEADER"

使い方:
    python backfill_daily.py jma_daily.dump [--dry-run]

方針:
  - observations.nc に既に値がある日×地点は**上書きしない**（現行の確定値 CSV 蓄積を優先）
  - 未知の地点コード（廃止地点など）は行を新規割当（amedas=NULL、名前は station_codes.json から補完）
  - 1 年ずつまとめて書き込み（コピー → 更新 → rename でクラッシュ耐性）
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import time
import warnings
from collections import defaultdict
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np

from weatherlib.ncstore import FILL, FILL_B, NcStore, date_index
from weatherlib.store import open_store

BASE = Path(__file__).resolve().parent
SQLITE = BASE / "store" / "weather.sqlite"
NC = BASE / "store" / "observations.nc"
MASTER = BASE / "master"

# jma_daily の列（WeatherToolsCore/JmaDaily2/Data/JmaDaily.cs と同じ）
COLS = ["地点コード", "観測日", "平均気温", "平均気温注", "最高気温", "最高気温注",
        "最低気温", "最低気温注", "降水量", "降水無", "降水量注"]

# nc 変数 ← (値列, 品質列) の対応
FIELD_MAP = {
    "tavg": ("平均気温", "平均気温注"),
    "tmax": ("最高気温", "最高気温注"),
    "tmin": ("最低気温", "最低気温注"),
    "precip": ("降水量", "降水量注"),
}
Q_VARS = {"tavg": "tavg_q", "tmax": "tmax_q", "tmin": "tmin_q", "precip": "precip_q"}


def log(msg: str) -> None:
    print(f"[backfill] {msg}", flush=True)


# ---------------------------------------------------------------- 入力の読み込み

def read_rows(path: Path):
    """pg_dump COPY 形式 / CSV を自動判別して {列名: 文字列} を順に返す。"""
    with path.open(encoding="utf-8", errors="replace") as f:
        head = f.readline()
        f.seek(0)
        if head.startswith("地点コード") or head.lower().startswith('"地点コード"'):
            # CSV (HEADER つき)
            for r in csv.DictReader(f):
                yield r
            return
        # pg_dump: 「COPY ... (列リスト) FROM stdin;」〜「\.」のタブ区切り
        cols = None
        for line in f:
            if cols is None:
                m = re.match(r"COPY\s+\S*jma_daily\S*\s*\(([^)]+)\)\s+FROM stdin;", line)
                if m:
                    cols = [c.strip().strip('"') for c in m.group(1).split(",")]
                continue
            if line.startswith("\\."):
                cols = None
                continue
            vals = line.rstrip("\n").split("\t")
            yield {c: (None if v == "\\N" else v) for c, v in zip(cols, vals)}


def to_int(s) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 行割当

def resolve_rows(conn, codes: set[int]) -> dict[int, int]:
    """code → nc 行番号。未知の code（廃止地点など）は新規割当する。"""
    known = dict(conn.execute("SELECT code, row FROM stations WHERE code IS NOT NULL"))
    missing = sorted(codes - set(known))
    if not missing:
        return known

    # 名前の補完（station_codes.json は廃止地点も含む）
    names = {}
    scp = MASTER / "station_codes.json"
    if scp.exists():
        for e in json.loads(scp.read_text(encoding="utf-8"))["entries"]:
            names.setdefault(int(e["block_no"]), e["name"])

    next_row = conn.execute("SELECT COALESCE(MAX(row), -1) + 1 FROM stations").fetchone()[0]
    for code in missing:
        conn.execute("INSERT INTO stations (row, code, name) VALUES (?, ?, ?)",
                     (next_row, code, names.get(code)))
        known[code] = next_row
        next_row += 1
    conn.commit()
    log(f"未知の地点コードに行を新規割当: {len(missing)} 件 "
        f"{[(c, names.get(c, '?')) for c in missing[:5]]}{'...' if len(missing) > 5 else ''}")
    return known


# ---------------------------------------------------------------- 本体

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return 1
    src = Path(args[0])
    started = time.monotonic()

    # 1. 読み込み・年別に整理
    by_year: dict[int, list] = defaultdict(list)
    codes: set[int] = set()
    n_rows = 0
    for r in read_rows(src):
        code = to_int(r.get("地点コード"))
        d_s = r.get("観測日", "")[:10]
        if code is None or len(d_s) != 10:
            continue
        d = date.fromisoformat(d_s)
        by_year[d.year].append((code, d, r))
        codes.add(code)
        n_rows += 1
    if not n_rows:
        log("✗ 有効な行がありません（形式を確認してください）")
        return 1
    years = sorted(by_year)
    log(f"入力: {n_rows:,} 行 / {len(codes)} 地点 / {years[0]}〜{years[-1]} 年")
    if dry:
        return 0

    # 2. 行割当と nc 書き込み（コピー → 更新 → rename）
    conn = open_store(SQLITE)
    work = NC.with_suffix(".nc.work")
    if NC.exists():
        shutil.copy2(NC, work)
    ncs = NcStore(work, conn)
    ds = ncs.ds

    n_write = n_keep = 0
    try:
        for y in years:
            rows_y = by_year[y]
            j0 = date_index(date(y, 1, 1))
            n_days = date_index(date(y, 12, 31)) - j0 + 1
            # 対象地点の行番号（年内に登場する code のみ）
            rowmap = resolve_rows(conn, {c for c, _, _ in rows_y})
            ncs.sidx = dict(conn.execute(
                "SELECT amedas, row FROM stations WHERE amedas IS NOT NULL"))
            nst = max(rowmap.values()) + 1
            nst = max(nst, ds.dimensions["station"].size)

            # 既存データを読み（無ければ FILL）、空セルだけ埋める
            blocks = {}
            has_dates = ds.dimensions["date"].size
            for var in list(FIELD_MAP) + list(Q_VARS.values()) + ["precip_none"]:
                if has_dates > j0:
                    cur = ds[var][:nst, j0:min(j0 + n_days, has_dates)]
                    pad = n_days - cur.shape[1]
                    fill = FILL if ds[var].dtype.itemsize == 2 else FILL_B
                    if pad > 0 or cur.shape[0] < nst:
                        full = np.full((nst, n_days), fill, dtype=ds[var].dtype)
                        full[:cur.shape[0], :cur.shape[1]] = cur
                        cur = full
                else:
                    fill = FILL if ds[var].dtype.itemsize == 2 else FILL_B
                    cur = np.full((nst, n_days), fill, dtype=ds[var].dtype)
                blocks[var] = cur

            for code, d, r in rows_y:
                row = rowmap[code]
                dj = date_index(d) - j0
                for var, (vcol, qcol) in FIELD_MAP.items():
                    v = to_int(r.get(vcol))
                    q = to_int(r.get(qcol))
                    if v is None or v == -999 or (q is not None and q < 1):
                        continue
                    if blocks[var][row, dj] != FILL:
                        n_keep += 1          # 既存値（現行蓄積）を優先
                        continue
                    blocks[var][row, dj] = v
                    if q is not None:
                        blocks[Q_VARS[var]][row, dj] = q
                    n_write += 1
                pn = to_int(r.get("降水無"))
                if pn is not None and blocks["precip_none"][row, dj] == FILL_B:
                    blocks["precip_none"][row, dj] = pn

            for var, arr in blocks.items():
                ds[var][:nst, j0:j0 + n_days] = arr
            ds["date"][j0:j0 + n_days] = np.arange(j0, j0 + n_days, dtype=np.int32)
            log(f"  {y} 年: {len(rows_y):,} 行を反映")
    finally:
        ncs.close()
        conn.close()

    work.replace(NC)
    import os
    log(f"完了: 値 {n_write:,} セルを追加（既存優先でスキップ {n_keep:,}）、"
        f"observations.nc = {os.path.getsize(NC) / 1e6:.1f} MB "
        f"({time.monotonic() - started:.1f} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
