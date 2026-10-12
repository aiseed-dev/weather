# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""数値予報パックの publisher（サーバー処理側）。

ECMWF Open Data の bulk GRIB を取得し、int16 パックの NetCDF に変換して
Cloudflare R2 へ置ける形のディレクトリツリーを作る。設計は
docs/forecast-distribution.md。アップロードは分離（rclone 等で行う）。

    ./.venv/bin/python tools/publish_forecast.py --out ./publish
    ./.venv/bin/python tools/publish_forecast.py --out ./publish --steps 0,24 --tier core
    ./.venv/bin/python tools/publish_forecast.py --out ./publish --tier core --charts   # 運用

- --charts を付けると、同じ GRIB で ECMWF のチャートも描く（publish_charts.render_step）。
  取得は 1 回で済む（律速は取得。1 ステップ 149MB）。
- 取得はスレッドで先へ進め、decode・書き・描画は --workers 個のプロセスで並べる。

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
import multiprocessing
import queue
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
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

# 1 ファイルに入れるサーフェス変数の上限。Cloudflare Pages の 25 MiB 制限に対し、
# 0.25° 全球 1 フィールド ≈ 0.93MB（int16+zlib 実測）なので 16 変数で約 15MB。
MAX_FIELDS_PER_PART = 16

# 処理（decode・書き・描画）を並べるプロセス数と、それに加えて先に取っておく GRIB の本数
DEFAULT_WORKERS = 3
PREFETCH = 1

ATTRIBUTION = "Data: ECMWF Open Data. CC-BY-4.0. https://www.ecmwf.int/en/forecasts/datasets/open-data"


# 取得が止まったと見なすまでの秒数。GCS からの bulk GRIB は、回線が遅い時間帯に途中で
# 止まることがある（2026-10-13 実測: 300 秒待ってから取り直して 1 本 388 秒）。
# 止まったら短く切り、受け取った所から Range で続きを取る。
READ_TIMEOUT = 30


def http_get(url: str, target: Path, retries: int = 8) -> None:
    """1 ファイル取得。止まり・503・切断には、受け取った所から続きを取る（指数バックオフ）。

    途中までの中身は target の隣の .part に置き、取り終えてから target に替える。
    取り切れなかったときは .part を消す（150MB の取り残しを作らない）。"""
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    total = None
    delay = 2.0
    try:
        for attempt in range(retries + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "aiseed-weather-publisher"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                    if have and resp.status != 206:      # 続きを返さない相手なら最初から
                        have = 0
                    if total is None:
                        length = resp.headers.get("Content-Length")
                        total = have + int(length) if length else None
                    # read1 で届いた分ずつ書く（大きな read は、止まると途中まで受けた分も捨てる）
                    with part.open("ab" if have else "wb") as f:
                        while chunk := resp.read1(1 << 16):
                            f.write(chunk)
                got = part.stat().st_size
                if total is None or got == total:
                    part.replace(target)
                    return
                raise OSError(f"途中で切れた（{got}/{total} バイト）")
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and total and have == total:   # 取り切っていた
                    part.replace(target)
                    return
                if exc.code < 500 and exc.code != 429:
                    raise
                err = exc
            except (urllib.error.URLError, OSError) as exc:
                err = exc
            if attempt == retries:
                raise err
            logger.warning("retry %s in %.0fs (%s バイト受信済み): %s", url.rsplit("/", 1)[-1], delay,
                           part.stat().st_size if part.exists() else 0, err)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    finally:
        part.unlink(missing_ok=True)


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


def _chunked(items: "list[str]", size: int) -> "list[list[str]]":
    return [items[i:i + size] for i in range(0, len(items), size)]


def split_datasets(sfc: "xr.Dataset | None", pl: "xr.Dataset | None",
                   sol: "xr.Dataset | None") -> "dict[str, xr.Dataset]":
    """kind 別 Dataset を配信単位へ割る。GRIB decode と切り離してある（テスト用）。

    配信単位は **1 ファイル 25 MiB 以下**に収める必要がある（Cloudflare Pages の
    ハード上限。有料プランでも変わらない）。0.25° 全球の 1 フィールドは
    int16+zlib で約 0.93MB なので、気圧面は面ごとに、サーフェスの ext は
    MAX_FIELDS_PER_PART 変数ずつに割る。

    - sfc-core          : 定番 6 変数（約 7MB）
    - sfc-ext-{n}       : 残りのサーフェス変数（各 MAX_FIELDS_PER_PART 変数まで）
    - pl-core-{level}   : 定番面の定番変数（各約 7MB）
    - pl-ext-{level}    : 定番面は残り変数のみ（約 4MB）、定番外の面は
                          全変数まとめて（約 10MB。旧 pl-ext-lv 相当を含む）
    - sol-core          : 土壌層（あれば）。アプリの取得ループは
                          kind = sfc/pl/sol を常に取る（_ACTIVE_KINDS）ので
                          mirror では sol も core 必須。約 7MB/步と小さい

    名前は `{kind}-{tier}[-{suffix}]` の形を保つこと。publish_run の tier 振り分けと
    再開判定が 2 番目のトークンを tier として読むため、崩すと両方が壊れる。
    """
    out: dict[str, xr.Dataset] = {}

    if sfc is not None:
        core = [v for v in CORE_SFC if v in sfc.data_vars]
        out["sfc-core"] = sfc[core]
        ext = [v for v in sfc.data_vars if v not in core]
        for n, group in enumerate(_chunked(ext, MAX_FIELDS_PER_PART), start=1):
            out[f"sfc-ext-{n}"] = sfc[group]

    if pl is not None:
        core_vars = [v for v in CORE_PL_VARS if v in pl.data_vars]
        ext_vars = [v for v in pl.data_vars if v not in core_vars]
        for level in [float(l) for l in pl.isobaricInhPa.values]:
            at = pl.sel(isobaricInhPa=[level])      # 面次元は残す（結合時に揃える）
            tag = str(int(level))
            if level in CORE_PL_LEVELS:
                if core_vars:
                    out[f"pl-core-{tag}"] = at[core_vars]
                if ext_vars:
                    out[f"pl-ext-{tag}"] = at[ext_vars]
            elif len(at.data_vars):
                out[f"pl-ext-{tag}"] = at

    if sol is not None:
        out["sol-core"] = sol
    return out


def split_tiers(path: Path) -> "dict[str, xr.Dataset]":
    """bulk GRIB を {ファイル名部: Dataset} に分割する。

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

    sfc = xr.merge(sfc_parts, compat="override") if sfc_parts else None
    return split_datasets(sfc, pl, sol)


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


def _worker_init() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def process_step(run: datetime, step: int, tier: str, grib: Path, run_dir: Path,
                 out: Path, charts: bool) -> "dict[str, dict]":
    """1 ステップ・1 tier 分の仕事（別プロセスで動く）。パックを書き、charts なら同じ GRIB で
    ECMWF のチャートも描く。manifest に足す {ファイル名: {bytes, sha256}} を返す。"""
    entries = {}
    logger.info("step %3dh: decode + pack (%s)", step, tier)
    for part, ds in split_tiers(grib).items():
        if part.split("-")[1] != tier:
            continue
        fname = f"{step:03d}h-{part}.nc"
        target = run_dir / fname
        write_pack(ds, target, run, step)
        entries[fname] = {"bytes": target.stat().st_size, "sha256": sha256(target)}
    if charts:
        from publish_charts import render_step
        render_step("ecmwf", run, step, {"sfc": grib, "pl": grib}, out)
    return entries


def remove_grib(grib: Path) -> None:
    grib.unlink(missing_ok=True)
    for idx in grib.parent.glob(grib.name + ".*.idx"):   # cfgrib が隣に作る索引
        idx.unlink(missing_ok=True)


def publish_run(run: datetime, steps: list[int], tiers: list[str], out: Path,
                source: str, keep_grib: bool, *, charts: bool = False,
                workers: int = DEFAULT_WORKERS) -> None:
    """取得は 1 本のスレッドで順に進め、decode・書き・描画は workers 個の別プロセスで並べる。

    律速は回線（bulk GRIB 1 本 149MB）。取得を止めずに、取り終えたステップから処理する
    （2026-10-13 実測: 1 ステップの CPU はパック 約 3 秒＋チャート 約 10 秒で、取得 11〜36 秒の間に収まる）。
    先に取っておく GRIB は workers + PREFETCH 本まで（ディスクは 150MB × その本数）。
    """
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
        # このステップの tier ファイルが manifest にあれば処理済み
        # （manifest はステップの全パート書き出し後に更新される）
        todo = []
        for step in steps:
            if any(f"-{tier}" in f for f in manifest["steps"].get(str(step), {})):
                logger.info("step %3dh %s: 済み", step, tier)
            else:
                todo.append(step)
        with_charts = charts and tier == tiers[0]
        # GRIB は最後の tier を処理し終えたステップから消す
        # （core→ext の 2 パス間で保持しないと 150MB×65 の再取得になる。
        #   その間 grib-cache が最大 約10GB 育つ点は設計書に記載）
        drop = not keep_grib and tier == tiers[-1]

        def record(step: int, grib: Path, entries: dict) -> None:
            manifest["steps"].setdefault(str(step), {}).update(entries)
            if with_charts and step not in manifest.setdefault("charts", []):
                manifest["charts"].append(step)
            if drop:
                remove_grib(grib)
            dump_json(manifest, manifest_path)

        _pipeline(todo, run, tier, grib_dir, run_dir, out, source, with_charts, workers, record)

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
        if with_charts:
            from publish_charts import model_entry, update_latest
            update_latest(out, {"ecmwf": model_entry("ecmwf", run, sorted(manifest["charts"]))})
            logger.info("ECMWF チャート %d ステップ → charts/latest.json 更新", len(manifest["charts"]))


def _pipeline(todo: "list[int]", run: datetime, tier: str, grib_dir: Path, run_dir: Path,
              out: Path, source: str, charts: bool, workers: int, record) -> None:
    """todo のステップを、取得スレッド → 処理プロセスの流れで片付ける。record は親で順に呼ぶ。"""
    if not todo:
        return
    slots = threading.BoundedSemaphore(workers + PREFETCH)
    ready: "queue.Queue" = queue.Queue()
    stop = threading.Event()

    def downloader() -> None:
        try:
            for step in todo:
                slots.acquire()
                if stop.is_set():
                    return
                grib = grib_dir / f"{step}h.grib2"
                if not grib.exists() or grib.stat().st_size == 0:
                    url = _bulk_url(source, run, step)
                    t0 = time.time()
                    http_get(url, grib)
                    logger.info("step %3dh: GRIB 取得 %.0f 秒 (%s)", step, time.time() - t0,
                                url.rsplit("/", 1)[-1])
                ready.put((step, grib))
            ready.put(None)
        except BaseException as exc:  # noqa: BLE001 — 親で投げ直す
            ready.put(exc)

    # fork はスレッド（取得）が動いている親を複製するので使わない
    ex = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_worker_init)
    pending: dict = {}

    def collect(block: bool) -> None:
        done_now = [f for f in pending if f.done()]
        if block and not done_now and pending:
            done_now = [next(as_completed(pending))]
        for fut in done_now:
            step, grib = pending.pop(fut)
            record(step, grib, fut.result())
            slots.release()

    threading.Thread(target=downloader, daemon=True).start()
    try:
        fetching = True
        while fetching or pending:
            if fetching:
                try:
                    item = ready.get(timeout=1.0)
                except queue.Empty:
                    collect(block=False)
                    continue
                if item is None:
                    fetching = False
                elif isinstance(item, BaseException):
                    raise item
                else:
                    step, grib = item
                    pending[ex.submit(process_step, run, step, tier, grib, run_dir, out, charts)] = (step, grib)
                collect(block=False)
            else:
                collect(block=True)
    finally:
        stop.set()
        try:
            slots.release()          # 取得スレッドが空きを待っていれば起こして終わらせる
        except ValueError:
            pass
        ex.shutdown(wait=True, cancel_futures=True)


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
    ap.add_argument("--charts", action="store_true",
                    help="同じ GRIB で ECMWF のチャート（publish_charts と同じ製品）も描く。"
                         "GFS・ENS は publish_charts --model gfs で別に描く")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"decode・書き・描画のプロセス数（既定 {DEFAULT_WORKERS}）")
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
    publish_run(run, steps, tiers, out, args.source, args.keep_grib,
                charts=args.charts, workers=max(1, args.workers))
    prune_runs(out, args.runs_keep, args.keep_grib)
    logger.info("完了。アップロード例: rclone sync %s/forecast r2:<bucket>/forecast", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
