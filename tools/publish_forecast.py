# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""数値予報パックの publisher（サーバー処理側）。

ECMWF Open Data の bulk GRIB を取得し、int16 パックの NetCDF に変換して
Cloudflare R2 へ置ける形のディレクトリツリーを作る。設計は
docs/forecast-distribution.md。アップロードは分離（rclone 等で行う）。

    ./.venv/bin/python tools/publish_forecast.py --out ./publish
    ./.venv/bin/python tools/publish_forecast.py --out ./publish --steps 0,24 --tier core

- tier "core" = 定番チャートの組（地上・500/300hPa高度・850hPa気温風・
  700hPa湿数/鉛直流・250hPaジェット）。先に全ステップ分を公開する。
- tier "ext"  = bulk GRIB に入っている残り全部（全サーフェス変数と
  全気圧面×全パラメータ、土壌層）。
- latest.json は tier の完成時点でだけ書き換わる（置き途中を見せない）。

アプリ本体の decode（cfgrib のハイパーキューブ分割・変数名の扱い）と
ダウンロード URL は services/forecast_service.py の実装をそのまま使う。
パック内の変数名は cfgrib 名（t2m, u10, …）。表示側は decode 後に
_restore_grib_shortnames を適用して GRIB native 名（2t, 10u）も引けるようにする。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import xarray as xr

from aiseed_weather.services.forecast_service import _bulk_url

logger = logging.getLogger("publish_forecast")

# IFS oper の公開ステップ: 0-144h は 3 時間刻み、150-240h は 6 時間刻み
ALL_STEPS = list(range(0, 145, 3)) + list(range(150, 241, 6))

FILL = np.int16(-32767)

# 定番チャートの組（ユーザー決定: よく使うセットを優先作成）
CORE_SFC = ["msl", "t2m", "u10", "v10", "tp", "tcc"]
CORE_PL_VARS = ["gh", "t", "u", "v", "r", "w"]
CORE_PL_LEVELS = [250.0, 300.0, 500.0, 700.0, 850.0]

ATTRIBUTION = "Data: ECMWF Open Data. CC-BY-4.0. https://www.ecmwf.int/en/forecasts/datasets/open-data"


def http_get(url: str, target: Path, retries: int = 3) -> None:
    """1 ファイル取得。503/切断に指数バックオフ（実測で必須と判明済み）。"""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aiseed-weather-publisher"})
            with urllib.request.urlopen(req, timeout=300) as resp, \
                    tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                shutil.copyfileobj(resp, tmp, length=1 << 20)
                Path(tmp.name).replace(target)
            return
        except (urllib.error.URLError, OSError) as exc:
            if attempt == retries:
                raise
            logger.warning("retry %s in %.0fs: %s", url, delay, exc)
            time.sleep(delay)
            delay *= 2


def url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "aiseed-weather-publisher"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False


def latest_complete_run(source: str) -> datetime:
    """最終ステップ(240h)のファイルが存在する最新ランを HEAD で探す。

    公開は概ねラン後 7-9 時間で完了するので、now-7h から 6 時間刻みで
    さかのぼる。"""
    now = datetime.now(timezone.utc) - timedelta(hours=7)
    cand = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for _ in range(8):
        if url_exists(_bulk_url(source, cand, ALL_STEPS[-1])):
            return cand
        cand -= timedelta(hours=6)
    raise RuntimeError("直近 2 日に完全公開されたランが見つからない")


def pack_encoding(ds: xr.Dataset) -> dict:
    """変数ごとの CF int16 パッキング encoding を計算する。

    scale/offset はデータの実レンジから決める（往復誤差はレンジ/65534、
    ERA5 気候値パックでゼロ実用誤差を確認済みの方式）。"""
    enc = {}
    for name, da in ds.data_vars.items():
        vals = da.values
        finite = np.isfinite(vals)
        if not finite.any():
            vmin, vmax = 0.0, 0.0
        else:
            vmin = float(vals[finite].min())
            vmax = float(vals[finite].max())
        # 65000 分割: 符号端(±32767)に寄せないことで FILL(-32767) との
        # 衝突と丸めの飛び出しを避ける。往復誤差はレンジ/65000
        scale = (vmax - vmin) / 65000.0 or 1.0
        enc[name] = {
            "dtype": "int16",
            "scale_factor": scale,
            "add_offset": (vmax + vmin) / 2.0,
            "_FillValue": FILL,
            "zlib": True,
            "complevel": 4,
            "shuffle": True,
        }
    return enc


def split_tiers(path: Path) -> "dict[str, xr.Dataset]":
    """bulk GRIB を {ファイル名部: Dataset} に分割する。

    - sfc-core / sfc-ext : サーフェス（core は定番 6 変数、ext は残り全部）
    - pl-core / pl-ext   : 気圧面（core は定番変数×定番面、ext は残り全部。
                           定番変数の残り面は pl-ext-lv）
    - sol-core           : 土壌層（あれば）。アプリの取得ループは
                           kind = sfc/pl/sol を常に取る（_ACTIVE_KINDS）ので
                           mirror では sol も core 必須。約 7MB/步と小さい

    cfgrib.open_datasets（150MB の decode。ここが一番重い）は 1 回だけ呼び、
    kind への振り分けは forecast_service._decode_kind と同じ規則で行う。
    パックには cfgrib 名（t2m 等）だけを入れる。GRIB native 名（2t）の
    alias 復元は表示側が decode 後に行う。
    """
    import cfgrib

    dss = cfgrib.open_datasets(str(path))
    pl = None
    sol = None
    sfc_parts = []
    for d in dss:
        if "isobaricInhPa" in d.dims:
            pl = d
        elif "soilLayer" in d.dims or "depthBelowLandLayer" in d.dims:
            sol = d
        else:
            sfc_parts.append(d)

    out: dict[str, xr.Dataset] = {}

    if sfc_parts:
        sfc = xr.merge(sfc_parts, compat="override")
        core = [v for v in CORE_SFC if v in sfc.data_vars]
        out["sfc-core"] = sfc[core]
        ext = [v for v in sfc.data_vars if v not in core]
        if ext:
            out["sfc-ext"] = sfc[ext]

    if pl is not None:
        core_vars = [v for v in CORE_PL_VARS if v in pl.data_vars]
        core_levels = [l for l in CORE_PL_LEVELS if l in pl.isobaricInhPa.values]
        out["pl-core"] = pl[core_vars].sel(isobaricInhPa=core_levels)
        ext_vars = [v for v in pl.data_vars if v not in core_vars]
        if ext_vars:
            out["pl-ext"] = pl[ext_vars]
        other_levels = [float(l) for l in pl.isobaricInhPa.values
                        if l not in core_levels]
        if core_vars and other_levels:
            out["pl-ext-lv"] = pl[core_vars].sel(isobaricInhPa=other_levels)

    if sol is not None:
        out["sol-core"] = sol
    return out


def write_pack(ds: xr.Dataset, target: Path, run: datetime, step: int) -> None:
    ds = ds.copy()
    ds.attrs.update({
        "source": ATTRIBUTION,
        "model": "ifs-0p25-oper",
        "run": f"{run:%Y-%m-%dT%H}Z",
        "step_hours": step,
        "Conventions": "CF-1.10",
    })
    tmp = target.with_suffix(".tmp")
    ds.to_netcdf(tmp, format="NETCDF4", encoding=pack_encoding(ds))
    tmp.replace(target)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path, default):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def dump_json(obj, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def publish_run(run: datetime, steps: list[int], tiers: list[str], out: Path,
                source: str, keep_grib: bool) -> None:
    run_name = f"{run:%Y%m%d_%H}z"
    run_dir = out / "forecast" / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    manifest = load_json(manifest_path, {
        "model": "ifs-0p25-oper", "run": f"{run:%Y-%m-%dT%H}Z",
        "attribution": ATTRIBUTION, "steps": {}, "tiers": {},
    })

    grib_dir = out / "grib-cache" / run_name
    grib_dir.mkdir(parents=True, exist_ok=True)

    for tier in tiers:  # core を全ステップ終えてから ext（優先公開）
        for step in steps:
            done = manifest["steps"].get(str(step), {})
            # このステップの tier ファイルが manifest にあれば処理済み
            # （manifest はステップの全パート書き出し後に更新される）
            if any(f"-{tier}" in f for f in done):
                logger.info("step %3dh %s: 済み", step, tier)
                continue

            grib = grib_dir / f"{step}h.grib2"
            if not grib.exists() or grib.stat().st_size == 0:
                url = _bulk_url(source, run, step)
                logger.info("step %3dh: GRIB 取得 %s", step, url.rsplit("/", 1)[-1])
                http_get(url, grib)

            logger.info("step %3dh: decode + pack (%s)", step, tier)
            for part, ds in split_tiers(grib).items():
                if part.split("-")[1] != tier:
                    continue
                fname = f"{step:03d}h-{part}.nc"
                target = run_dir / fname
                write_pack(ds, target, run, step)
                entry = manifest["steps"].setdefault(str(step), {})
                entry[fname] = {"bytes": target.stat().st_size, "sha256": sha256(target)}

            # GRIB は最後の tier を処理し終えたステップから消す
            # （core→ext の 2 パス間で保持しないと 150MB×65 の再取得になる。
            #   その間 grib-cache が最大 約10GB 育つ点は設計書に記載）
            if not keep_grib and tier == tiers[-1]:
                grib.unlink(missing_ok=True)
            dump_json(manifest, manifest_path)

        # tier 完成 → manifest と latest.json に反映（段階公開）
        manifest["tiers"][tier] = True
        dump_json(manifest, manifest_path)
        latest = out / "forecast" / "latest.json"
        dump_json({
            "model": "ifs-0p25-oper",
            "run": f"{run:%Y-%m-%dT%H}Z",
            "base": f"runs/{run_name}",
            "steps": steps,
            "tiers": manifest["tiers"],
        }, latest)
        logger.info("tier %s 完成 → latest.json 更新", tier)


def prune_runs(out: Path, keep: int, keep_grib: bool) -> None:
    runs_dir = out / "forecast" / "runs"
    if not runs_dir.is_dir():
        return
    runs = sorted(d for d in runs_dir.iterdir() if d.is_dir())
    for old in runs[:-keep] if keep > 0 else []:
        logger.info("古いランを削除: %s", old.name)
        shutil.rmtree(old)
    if not keep_grib:
        shutil.rmtree(out / "grib-cache", ignore_errors=True)


def parse_steps(text: str) -> list[int]:
    if text == "all":
        return ALL_STEPS
    return [int(s) for s in text.split(",")]


def main() -> int:
    ap = argparse.ArgumentParser(description="ECMWF 数値予報 → Cloudflare 配布パック生成")
    ap.add_argument("--out", default="publish", help="出力ルート（この下に forecast/ を作る）")
    ap.add_argument("--steps", default="all", help="'all' か カンマ区切り時数（例 0,24,48）")
    ap.add_argument("--tier", choices=["core", "ext", "both"], default="both")
    ap.add_argument("--run", help="YYYYMMDDHH 指定（省略時は最新の完全公開ラン）")
    ap.add_argument("--source", default="google", choices=["google", "aws", "azure", "ecmwf"])
    ap.add_argument("--runs-keep", type=int, default=2)
    ap.add_argument("--keep-grib", action="store_true",
                    help="取得した GRIB を残す（デバッグ用。既定はパック後に削除）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(args.out).resolve()

    if args.run:
        run = datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    else:
        run = latest_complete_run(args.source)
    logger.info("対象ラン: %s", f"{run:%Y-%m-%d %H}Z")

    steps = parse_steps(args.steps)
    tiers = ["core", "ext"] if args.tier == "both" else [args.tier]
    publish_run(run, steps, tiers, out, args.source, args.keep_grib)
    prune_runs(out, args.runs_keep, args.keep_grib)
    logger.info("完了。アップロード例: rclone sync %s/forecast r2:<bucket>/forecast", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
