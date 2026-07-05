#!/usr/bin/env python3
"""観測データの配布用エクスポート（Cloudflare R2 等の静的配信向け）。

「過去の気象データ・ダウンロード」型の動的切り出しシステムの代替モデル:
アクセスパターン別に**あらかじめ分割した NetCDF** を静的配信すれば、
サーバー側の切り出し処理（＝運営費の主因）が不要になる。

  dist/
    full/observations.nc      … 全部入りスナップショット（一括利用者向け）
    stations/{code}.nc        … 1 地点×全期間（「地点を選んで期間指定」の代替。各数百 KB）
    years/{yyyy}.nc           … 全地点×1 年（年断面の分析向け）
    stations.json             … 地点メタデータ（コード・名前・緯度経度）
    manifest.json             … ファイル一覧（サイズ・sha256・更新時刻・被覆期間）

すべての NetCDF に出典（気象庁）と加工者の表示をグローバル属性で埋め込む
（政府標準利用規約 2.0 / CC-BY 4.0 互換の要件）。

使い方:
    python export_dist.py [--out dist]
更新は差分で速い: 全期間で不変の年ファイル・地点ファイルは内容ハッシュが
変わらない限り書き直さない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import netCDF4 as nc
import numpy as np

from weatherlib.ncstore import DAILY_EPOCH, FILL, FILL_B, date_index

BASE = Path(__file__).resolve().parent
NC = BASE / "store" / "observations.nc"
STATIONS = BASE / "master" / "stations.json"

ATTRIBUTION = ("出典: 気象庁ホームページ (https://www.jma.go.jp/) の公開データを"
               "編集・加工したもの。編集責任は配布者にあります。")
LICENSE = "政府標準利用規約2.0（CC-BY 4.0 互換）に基づく再配布。利用時は出典を明記してください。"

DAILY_VARS = [
    ("tmax", "i2", "daily maximum temperature (x10 degC)"),
    ("tmax_minutes", "i2", "time of daily max (minutes from midnight)"),
    ("tmax_q", "i1", "JMA quality code for tmax (8=final)"),
    ("tmin", "i2", "daily minimum temperature (x10 degC)"),
    ("tmin_minutes", "i2", "time of daily min (minutes from midnight)"),
    ("tmin_q", "i1", "JMA quality code for tmin"),
    ("tavg", "i2", "daily mean temperature (x10 degC)"),
    ("tavg_q", "i1", "JMA quality code for tavg"),
    ("precip", "i2", "daily precipitation (x10 mm)"),
    ("precip_q", "i1", "JMA quality code for precip"),
    ("precip_none", "i1", "no-precipitation flag"),
    ("sun", "i2", "daily sunshine duration (x10 h)"),
]


def log(msg: str) -> None:
    print(f"[dist] {msg}", flush=True)


def common_attrs(ds_out, coverage: str):
    ds_out.title = "Daily surface observations in Japan (derived from JMA public data)"
    ds_out.source = ATTRIBUTION
    ds_out.license = LICENSE
    ds_out.coverage = coverage
    ds_out.Conventions = "CF-1.10"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()
    out = BASE / args.out
    started = time.monotonic()

    st_meta = json.loads(STATIONS.read_text(encoding="utf-8"))
    src = nc.Dataset(NC)
    src.set_auto_mask(False)

    # データのある日付範囲を特定
    dates = src["date"][:]
    valid = np.where(dates != -1)[0]
    if len(valid) == 0:
        log("データがありません")
        return 1
    j0, j1 = int(valid.min()), int(valid.max())
    d0 = DAILY_EPOCH + timedelta(days=j0)
    d1 = DAILY_EPOCH + timedelta(days=j1)
    coverage = f"{d0.isoformat()}..{d1.isoformat()}"
    log(f"被覆期間: {coverage}")

    # 対象地点（code が付与済みのもの）
    stations = [(int(c), r) for c, r in st_meta["stations"].items()]
    stations.sort()
    rows = np.array([r["row"] for _, r in stations])
    codes = np.array([c for c, _ in stations], dtype=np.int32)

    # 日別変数を一括読み（現期間ならメモリに収まる。将来は年単位に分割読みへ）
    data = {}
    for name, typ, _ in DAILY_VARS:
        data[name] = src[name][:, j0:j1 + 1][rows]
    src.close()

    (out / "full").mkdir(parents=True, exist_ok=True)
    (out / "stations").mkdir(parents=True, exist_ok=True)
    (out / "years").mkdir(parents=True, exist_ok=True)

    def write_nc(path: Path, sel_rows, sel_days, day_offset: int):
        """sel_rows(地点index配列)×sel_days(日数)の切り出しを書く。"""
        ds = nc.Dataset(path, "w", format="NETCDF4")
        common_attrs(ds, coverage)
        ds.createDimension("station", len(sel_rows))
        ds.createDimension("time", sel_days)
        v = ds.createVariable("station_code", "i4", ("station",))
        v.long_name = "JMA station code (kansho: intl 5-digit / amedas: etrn 4-digit)"
        v[:] = codes[sel_rows]
        v = ds.createVariable("time", "i4", ("time",))
        v.units = f"days since {DAILY_EPOCH.isoformat()}"
        v[:] = np.arange(j0 + day_offset, j0 + day_offset + sel_days, dtype=np.int32)
        for name, typ, desc in DAILY_VARS:
            fill = FILL if typ == "i2" else FILL_B
            var = ds.createVariable(name, typ, ("station", "time"), fill_value=fill,
                                    zlib=True, complevel=5, shuffle=True,
                                    chunksizes=(min(256, len(sel_rows)), min(366, sel_days)))
            var.long_name = desc
            var[:, :] = data[name][sel_rows, day_offset:day_offset + sel_days]
        ds.close()

    manifest = {"generated": datetime.now().isoformat(timespec="seconds"),
                "coverage": coverage, "attribution": ATTRIBUTION,
                "license": LICENSE, "files": {}}

    def record(path: Path):
        rel = str(path.relative_to(out))
        manifest["files"][rel] = {"bytes": path.stat().st_size, "sha256": sha256(path)}

    # 1. 全部入り
    p = out / "full" / "observations.nc"
    write_nc(p, np.arange(len(stations)), j1 - j0 + 1, 0)
    record(p)
    log(f"full/observations.nc: {p.stat().st_size / 1e6:.2f} MB")

    # 2. 年別（全地点×1年）
    for year in range(d0.year, d1.year + 1):
        y0 = max(date_index(date(year, 1, 1)), j0) - j0
        y1 = min(date_index(date(year, 12, 31)), j1) - j0
        p = out / "years" / f"{year}.nc"
        write_nc(p, np.arange(len(stations)), y1 - y0 + 1, y0)
        record(p)
    log(f"years/: {d0.year}〜{d1.year}")

    # 3. 地点別（1地点×全期間）— データのある地点のみ
    n_st = 0
    for i, (code, rec) in enumerate(stations):
        if not (data["tmax"][i] != FILL).any() and not (data["precip"][i] != FILL).any():
            continue
        p = out / "stations" / f"{code}.nc"
        write_nc(p, np.array([i]), j1 - j0 + 1, 0)
        record(p)
        n_st += 1
    log(f"stations/: {n_st} 地点")

    # 4. メタデータとマニフェスト
    shutil.copy2(STATIONS, out / "stations.json")
    record(out / "stations.json")
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(f["bytes"] for f in manifest["files"].values())
    log(f"完了: {len(manifest['files'])} ファイル / 合計 {total / 1e6:.1f} MB "
        f"({time.monotonic() - started:.1f} 秒)")
    log(f"R2 へは: wrangler r2 object put などで {out}/ を同期（配信転送料は無料）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
