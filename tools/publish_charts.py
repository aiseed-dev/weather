# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""数値予報チャート publisher（静的サイトの /Forecast/ 用画像を生成）。

設計: docs/forecast-charts.md。ECMWF(既定)/GFS の決定論チャート + ECMWF ENS の
降水アンサンブル 3 製品を PNG で書き出す。描画はアプリの figures/ を再利用。

  ECMWF: bulk GRIB（publish_forecast.py と grib-cache を共用可能）
  GFS  : AWS noaa-gfs-bdp-pds の .idx から必要フィールドだけ Range 取得し、
         変数名を ECMWF 流に正規化した NetCDF に変換 → 同じ描画経路
  ENS  : GCS ミラー enfo の .index から tp×全メンバーを Range 取得

    ./.venv/bin/python tools/publish_charts.py --out ./publish [--steps 0,24]
        [--model both|ecmwf|gfs|none] [--ens/--no-ens] [--run YYYYMMDDHH]

出力: {out}/charts/{model}/{product}/{step:03d}.png + charts/latest.json
（最新 1 ランのみ保持・上書き）
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import xarray as xr
from PIL import Image, ImageDraw, ImageFont

from publish_forecast import ALL_STEPS, http_get, pack_encoding
from aiseed_weather.services.forecast_service import _bulk_url

logger = logging.getLogger("publish_charts")

UA = {"User-Agent": "aiseed-weather-charts (+https://github.com/aiseed-dev/weather)"}
GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
ENS_STEPS = list(range(24, 241, 24))  # 24h 窓の終端
UPSCALE_MIN_W = 800   # これより小さい画像は 3 倍に拡大（ユーザー決定: 大きく見やすく）

# 製品カタログ: (product, layer_key, region_key, msl_overlay)
DET_PRODUCTS = [
    ("msl-precip", "tp",      "japan",  True),
    ("t2m",        "t2m",     "japan",  True),
    ("t850",       "t850",    "japan",  False),
    ("t500",       "t500",    "japan",  False),
    ("t925",       "t925",    "japan",  False),
    ("wind10m",    "wind10m", "japan",  False),
    ("wind300",    "wind300", "japan",  False),
    ("wind500",    "wind500", "japan",  False),
    ("t500-polar", "t500",    "arctic", False),
    ("t850-polar", "t850",    "arctic", False),
]
ENS_PRODUCTS = ["ens-tp-mean", "ens-tp-prob1", "ens-tp-prob30"]

ATTR = {"ecmwf": "ECMWF Open Data (CC-BY-4.0)",
        "gfs": "NOAA GFS (public domain)",
        "ens": "ECMWF Open Data ENS, 51 members (CC-BY-4.0)"}

# GFS .idx の (VAR, LEVEL) → 取得対象。cfgrib 名は ECMWF 流へ正規化する
GFS_WANT = {("PRMSL", "mean sea level"), ("TMP", "2 m above ground"),
            ("TMP", "850 mb"), ("TMP", "500 mb"), ("TMP", "925 mb"),
            ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
            ("UGRD", "300 mb"), ("VGRD", "300 mb"),
            ("UGRD", "500 mb"), ("VGRD", "500 mb"), ("APCP", "surface")}
GFS_RENAME = {"prmsl": "msl"}


def http_get_bytes(url: str, rng: "tuple[int, int] | None" = None) -> bytes:
    import urllib.request
    h = dict(UA)
    if rng:
        h["Range"] = f"bytes={rng[0]}-{rng[1]}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# ---------------------------------------------------------------- GFS

def fetch_gfs_step(run: datetime, step: int, work: Path) -> "tuple[Path, Path]":
    """GFS の必要フィールドを Range 取得し、正規化 NetCDF (sfc/pl) にする。

    戻り値は (sfc, pl) のパス。描画側の decode は mirror パックと同じ経路
    （figures は変更なし）。層の kind に応じて呼び出し側がパスを選ぶ。"""
    sfc_nc = work / f"{step:03d}h-sfc-core.nc"
    pl_nc = work / f"{step:03d}h-pl-core.nc"
    if sfc_nc.exists() and pl_nc.exists():
        return sfc_nc, pl_nc
    base = f"{GFS_BASE}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"
    lines = [l.split(":") for l in http_get_bytes(base + ".idx").decode().splitlines()]
    buf = b""
    for i, l in enumerate(lines):
        if (l[3], l[4]) in GFS_WANT:
            start = int(l[1])
            end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else None
            buf += http_get_bytes(base, (start, end))
    grib = work / f"gfs-{step:03d}.grib2"
    grib.write_bytes(buf)

    import cfgrib
    dss = cfgrib.open_datasets(str(grib))
    pl_parts, sfc_parts = [], []
    for d in dss:
        (pl_parts if "isobaricInhPa" in d.dims else sfc_parts).append(d)
    sfc = xr.merge(sfc_parts, compat="override").rename(
        {k: v for k, v in GFS_RENAME.items() if k in xr.merge(sfc_parts, compat="override").data_vars})
    pl = xr.merge(pl_parts, compat="override")
    for ds, path in ((sfc, sfc_nc), (pl, pl_nc)):
        ds.attrs["source"] = ATTR["gfs"]
        tmp = path.with_suffix(".tmp")
        ds.to_netcdf(tmp, format="NETCDF4", encoding=pack_encoding(ds))
        tmp.replace(path)
    grib.unlink()
    return sfc_nc, pl_nc


def latest_gfs_run(now: "datetime | None" = None) -> datetime:
    cand = (now or datetime.now(timezone.utc)) - timedelta(hours=5)
    cand = cand.replace(hour=(cand.hour // 12) * 12, minute=0, second=0, microsecond=0)
    import urllib.request, urllib.error
    for _ in range(4):
        url = f"{GFS_BASE}/gfs.{cand:%Y%m%d}/{cand:%H}/atmos/gfs.t{cand:%H}z.pgrb2.0p25.f240.idx"
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=30):
                return cand
        except urllib.error.HTTPError:
            cand -= timedelta(hours=12)
    raise RuntimeError("GFS の完全公開ランが見つからない")


# ---------------------------------------------------------------- ENS 降水

def fetch_ens_tp(run: datetime, step: int, work: Path) -> "np.ndarray | None":
    """enfo の tp を全メンバー分 Range 取得 → (member, lat, lon) [mm]。"""
    cache = work / f"ens-tp-{step:03d}.npy"
    if cache.exists():
        return np.load(cache)
    base = (f"https://storage.googleapis.com/ecmwf-open-data/{run:%Y%m%d}/{run:%H}z/"
            f"ifs/0p25/enfo/{run:%Y%m%d}{run:%H}0000-{step}h-enfo-ef")
    entries = [json.loads(l) for l in http_get_bytes(base + ".index").decode().splitlines()]
    tps = [e for e in entries if e.get("param") == "tp"]
    if not tps:
        return None
    buf = b"".join(http_get_bytes(base + ".grib2",
                                  (e["_offset"], e["_offset"] + e["_length"] - 1))
                   for e in tps)
    grib = work / f"ens-{step}.grib2"
    grib.write_bytes(buf)
    import cfgrib
    arrs = []
    for d in cfgrib.open_datasets(str(grib)):
        v = d["tp"].values * 1000.0  # m → mm
        arrs.append(v if v.ndim == 3 else v[None])
    grib.unlink()
    out = np.concatenate(arrs, axis=0).astype(np.float32)
    np.save(cache, out)
    return out


def render_ens(product: str, field: np.ndarray, lons, lats, run_id: str) -> bytes:
    """計算済み 2D フィールドをアプリの層別レンダラで描く。"""
    from aiseed_weather.figures._chart_spec import ChartSpec
    from aiseed_weather.figures._layered_renderer import render
    from aiseed_weather.figures.regions import JAPAN

    if product == "ens-tp-mean":
        spec_args = dict(vmin=0.0, vmax=100.0, anchors=(
            (0.0, (255, 255, 255)), (1.0, (200, 225, 255)), (10.0, (90, 160, 235)),
            (30.0, (30, 90, 200)), (60.0, (150, 60, 190)), (100.0, (230, 40, 60))),
            legend_ticks=(1.0, 10.0, 30.0, 60.0, 100.0),
            label="ENS mean 24h precip [mm]")
    else:
        spec_args = dict(vmin=0.0, vmax=100.0, anchors=(
            (0.0, (255, 255, 255)), (20.0, (170, 215, 250)), (50.0, (70, 150, 230)),
            (80.0, (240, 150, 40)), (100.0, (215, 30, 45))),
            legend_ticks=(20.0, 40.0, 60.0, 80.0, 100.0),
            label=f"P(24h precip) [%] {product}")
    spec = ChartSpec(layer_key=product, extractor=lambda _ds: field,
                     transparency=0.35, isolines=None, dry_threshold=0.5,
                     **spec_args)
    ds = xr.Dataset(coords={"longitude": ("longitude", lons),
                            "latitude": ("latitude", lats)})
    return render(spec, ds, region=JAPAN, run_id=run_id)


# ---------------------------------------------------------------- 仕上げ（拡大＋出典帯）

def finalize(png: bytes, caption: str, attribution: str) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width < UPSCALE_MIN_W:
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    strip_h = 26
    out = Image.new("RGB", (img.width, img.height + strip_h), (24, 34, 46))
    out.paste(img, (0, 0))
    d = ImageDraw.Draw(out)
    font = None
    for name in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                 "NotoSansCJK-Regular.ttc", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 13)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((8, img.height + 6), caption, fill=(235, 240, 245), font=font)
    right = f"{attribution}  aiseed.dev"
    w = d.textlength(right, font=font)
    d.text((img.width - w - 8, img.height + 6), right, fill=(160, 175, 190), font=font)
    buf = io.BytesIO()
    out.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


# ---------------------------------------------------------------- メイン

def render_model(model: str, run: datetime, steps: "list[int]", out: Path,
                 grib_cache: Path) -> "list[int]":
    from aiseed_weather.figures.render_pool import render_layer
    from aiseed_weather.figures.regions import JAPAN, ARCTIC
    from aiseed_weather.products.catalog import field_by_key
    regions = {"japan": JAPAN, "arctic": ARCTIC}
    done_steps = []
    work = grib_cache / model
    work.mkdir(parents=True, exist_ok=True)
    for step in steps:
        if model == "ecmwf":
            # bulk GRIB は kind 混載なので全製品同じパス
            path = work / f"{step}h.grib2"
            if not path.exists() or path.stat().st_size == 0:
                http_get(_bulk_url("google", run, step), path)
            path_for = {"sfc": path, "pl": path}
        else:
            sfc_nc, pl_nc = fetch_gfs_step(run, step, work)
            path_for = {"sfc": sfc_nc, "pl": pl_nc}
        run_id = f"{model.upper()} {run:%Y-%m-%d %H}Z +{step}h"
        for product, layer, region_key, overlay in DET_PRODUCTS:
            target = out / "charts" / model / product / f"{step:03d}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            kind = field_by_key(layer).kind
            png = render_layer(path_for[kind], regions[region_key], run_id=run_id,
                               layer_key=layer,
                               msl_overlay_path=path_for["sfc"] if overlay else None)
            target.write_bytes(finalize(
                png, f"{product}  {run:%Y-%m-%d %H}Z  T+{step}h", ATTR[model]))
        done_steps.append(step)
        logger.info("%s step %3dh: %d 製品", model, step, len(DET_PRODUCTS))
        if model == "ecmwf":
            pass  # bulk GRIB は publish_forecast と共用のため残す
    return done_steps


def render_ens_products(run: datetime, out: Path, grib_cache: Path,
                        windows: "list[int]") -> "list[int]":
    work = grib_cache / "ens"
    work.mkdir(parents=True, exist_ok=True)
    lons = np.arange(0, 360, 0.25, dtype=np.float32)
    lats = np.arange(90, -90.25, -0.25, dtype=np.float32)
    done = []
    prev = {0: None}
    for w_end in windows:
        cur = fetch_ens_tp(run, w_end, work)
        base = fetch_ens_tp(run, w_end - 24, work) if w_end > 24 else 0.0
        if cur is None:
            continue
        window = cur - (base if isinstance(base, np.ndarray) else 0.0)
        run_id = f"ECMWF ENS {run:%Y-%m-%d %H}Z T+{w_end - 24}..{w_end}h"
        fields = {"ens-tp-mean": window.mean(axis=0),
                  "ens-tp-prob1": (window >= 1.0).mean(axis=0) * 100.0,
                  "ens-tp-prob30": (window >= 30.0).mean(axis=0) * 100.0}
        for product, field in fields.items():
            png = render_ens(product, field.astype(np.float32), lons, lats, run_id)
            target = out / "charts" / "ens" / product / f"{w_end:03d}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            label = {"ens-tp-mean": "ENS平均 24h降水量",
                     "ens-tp-prob1": "24h降水 1mm以上の確率",
                     "ens-tp-prob30": "24h降水 30mm以上の確率"}[product]
            target.write_bytes(finalize(
                png, f"{label}  {run:%Y-%m-%d %H}Z  T+{w_end - 24}..{w_end}h",
                ATTR["ens"]))
        done.append(w_end)
        logger.info("ens 窓 T+%d..%dh: 3 製品", w_end - 24, w_end)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="数値予報チャート生成（/Forecast/ 用）")
    ap.add_argument("--out", default="publish")
    ap.add_argument("--steps", default="all", help="'all' か カンマ区切り時数")
    ap.add_argument("--model", choices=["both", "ecmwf", "gfs", "none"], default="both")
    ap.add_argument("--ens", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--run", help="YYYYMMDDHH（省略時は各モデルの最新完全ラン）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out = Path(args.out).resolve()
    grib_cache = out / "grib-cache-charts"
    steps = ALL_STEPS if args.steps == "all" else [int(s) for s in args.steps.split(",")]

    from publish_forecast import latest_complete_run
    latest = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "models": {}}
    models = {"both": ["ecmwf", "gfs"], "ecmwf": ["ecmwf"],
              "gfs": ["gfs"], "none": []}[args.model]
    for model in models:
        if args.run:
            run = datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        else:
            run = latest_complete_run("google") if model == "ecmwf" else latest_gfs_run()
        logger.info("%s ラン: %s", model, f"{run:%Y-%m-%d %H}Z")
        done = render_model(model, run, steps, out, grib_cache)
        latest["models"][model] = {
            "run": f"{run:%Y-%m-%dT%H}Z", "steps": done,
            "products": [p for p, *_ in DET_PRODUCTS], "attribution": ATTR[model]}

    if args.ens:
        if args.run:
            run = datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        else:
            run = latest_complete_run("google")
        windows = [w for w in ENS_STEPS if args.steps == "all" or w in steps or True][:len(ENS_STEPS)]
        if args.steps != "all":
            windows = [w for w in ENS_STEPS if w <= max(steps)] or [24]
        done = render_ens_products(run, out, grib_cache, windows)
        latest["models"]["ens"] = {
            "run": f"{run:%Y-%m-%dT%H}Z", "steps": done,
            "products": ENS_PRODUCTS, "attribution": ATTR["ens"]}

    lj = out / "charts" / "latest.json"
    lj.parent.mkdir(parents=True, exist_ok=True)
    lj.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("完了: %s", lj)
    return 0


if __name__ == "__main__":
    sys.exit(main())
