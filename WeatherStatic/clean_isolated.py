#!/usr/bin/env python3
"""日別の最高・最低気温から「孤立値」を探し、欠測に戻す。

孤立値: その地点で前後 WINDOW 日に同じ要素の値が 1 つも無いのに、その日だけ
値があるセル。気温を観測している地点は毎日値があるので、孤立値は気温を
観測していない地点（雨量計など）の行に入り込んだゴミとみなせる。

2026-08 の蓄積が libnetcdf の読み出しバグ（ncstore.normalize_extents 参照）で
ゴミ列を書き、repair_sentinels.py の選別（品質 1〜8・起時が妥当なら残す）を
たまたま通った破片が雨量計の行に残った（2026-08-28 tmin・08-29 tmax/tmin の
51 セル）。etrn は気温のページが無い地点を上書きしないので、取り直しでは
消えない。これを掃除するための道具で、普段の点検にも使える。

使い方（ストアのある dev2 で）:
    python clean_isolated.py --from 2026-08-01 --to 2026-09-30            # 下見
    python clean_isolated.py --from 2026-08-01 --to 2026-09-30 --apply    # 書き換え
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import netCDF4 as nc

from weatherlib.ncstore import DAILY_EPOCH, FILL, FILL_B, date_index, normalize_extents
from weatherlib.storelock import store_lock

BASE = Path(__file__).resolve().parent
VARS = ("tmax", "tmin")
WINDOW = 45                     # 前後この日数に同じ要素の値が無ければ孤立


def log(msg: str) -> None:
    print(f"[isolated] {msg}", flush=True)


def find_isolated(ds, j0: int, j1: int) -> dict[str, list[tuple[int, int]]]:
    """var → [(row, 日 index)]。読みは 1 行単位（常に正しい読み方）。"""
    nst = ds.dimensions["station"].size
    nd = ds.dimensions["date"].size
    w0, w1 = max(0, j0 - WINDOW), min(nd, j1 + WINDOW)
    out: dict[str, list[tuple[int, int]]] = {v: [] for v in VARS}
    for v in VARS:
        for r in range(nst):
            x = ds[v][r, w0:w1]
            has = x != FILL
            if not has.any():
                continue
            # 各セルについて「前後 WINDOW 日にある値の数」を累積和で数える
            c = np.concatenate([[0], np.cumsum(has)])
            for k in np.nonzero(has[j0 - w0:j1 - w0])[0] + (j0 - w0):
                lo, hi = max(0, k - WINDOW), min(len(x), k + WINDOW + 1)
                if c[hi] - c[lo] == 1:          # 自分しかいない
                    out[v].append((r, w0 + int(k)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="最高・最低気温の孤立値を欠測に戻す（既定は下見だけ）")
    ap.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    ap.add_argument("--store", type=Path, default=BASE / "store")
    ap.add_argument("--apply", action="store_true", help="実際に書き換える")
    args = ap.parse_args()

    nc_path = args.store / "observations.nc"
    sq_path = args.store / "weather.sqlite"
    for p in (nc_path, sq_path):
        if not p.exists():
            log(f"✗ ありません: {p}")
            return 1
    conn = sqlite3.connect(f"file:{sq_path}?mode=ro", uri=True)
    info = {r[0]: r[1:] for r in conn.execute(
        "SELECT row, amedas, COALESCE(name, '?') FROM stations")}
    conn.close()

    j0, j1 = date_index(args.start), date_index(args.end) + 1
    with store_lock(log=log):
        work = nc_path.with_suffix(".nc.work")
        if args.apply:
            shutil.copy2(nc_path, work)
        ds = nc.Dataset(work if args.apply else nc_path, "a" if args.apply else "r")
        ds.set_auto_mask(False)
        found = find_isolated(ds, j0, min(j1, ds.dimensions["date"].size))
        total = 0
        for v, cells in found.items():
            by_day: dict[str, int] = {}
            for r, j in cells:
                by_day[str(DAILY_EPOCH + timedelta(days=j))] = \
                    by_day.get(str(DAILY_EPOCH + timedelta(days=j)), 0) + 1
            log(f"{v}: 孤立値 {len(cells)} セル {by_day}")
            for r, j in cells[:8]:
                log(f"    row {r} {info.get(r)} {DAILY_EPOCH + timedelta(days=j)} "
                    f"値 {ds[v][r, j] / 10:.1f} 品質 {ds[v + '_q'][r, j]} "
                    f"起時 {ds[v + '_minutes'][r, j]}")
            if args.apply:
                for r, j in cells:
                    ds[v][r, j] = FILL
                    ds[v + "_q"][r, j] = FILL_B
                    ds[v + "_minutes"][r, j] = FILL
            total += len(cells)
        if args.apply:
            normalize_extents(ds)
        ds.close()
        if args.apply:
            work.replace(nc_path)

    if args.apply:
        log(f"完了: {total} セルを欠測に戻しました")
    else:
        log(f"下見だけです（{total} セルが対象。--apply で書き換え）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
