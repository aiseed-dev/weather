#!/usr/bin/env python3
"""アメダス 10 分値のアーカイブ化。生の JSON を NetCDF-4 へ封入する。

役割分担:
    収集 … workers/amedas/worker.js（Cloudflare Worker）が 10 分ごとに
           気象庁から取得し、**加工せずそのまま** R2 へ置く
    封入 … このスクリプト（tgsvr）が R2 から拾って NetCDF へまとめる。
           netCDF4 / numpy を使う処理は Workers の CPU 10ms・メモリ 128MB に
           載らないので、重い側はすべてここで行う


気象庁の map JSON は **約 10 日で消える**（実測: 10 日前 200 / 11 日前 404）。
etrn から後追いできるのは日別・時別の気温と降水だけで、湿度・気圧・視程・風・
10 分降水は失われる。取り逃した分は二度と取れないため 10 分毎に保存する。

出力（public_amedas/）:
    latest_time.txt              … 最新スロットの時刻（気象庁と同じ形式）
    latest.json                  … 最新スロット（気象庁の生ペイロードそのまま）
    map/{ts}.json                … 生ペイロード。気象庁と同じパス構造なので、
                                   参照先ホストを差し替えるだけで既存コードが動く
    archive/{YYYY}/{MM}-{1,2}.nc … 半月ごとの NetCDF-4（恒久保存・解析用。加工データ）
    index.json                   … 提供内容・更新時刻・出典
    _headers                     … CORS と Cache-Control

生 JSON の保持: 取得しに行くのは直近 WINDOW_DAYS 日（気象庁の保持期間に合わせる）。
公開ツリーからは**半月分を NetCDF へ封入した時点で**消すため、map/ には常に
10〜26 日分が載る（半月の期末が 10 日窓から外れて初めて封入できるため）。
気象庁より短くなることはない。

なぜアーカイブが NetCDF か（実測 2026-08-27、2026-08-25 の 144 スロットで比較）:
    生 JSON 36.75 MB → gzip JSON 2.03 MB → NetCDF-4(int16+zlib) 0.60 MB
  1 日を 1 チャンクに収めると隣接時刻の相関が効き、品質フラグ 13 要素を
  足したうえで gzip JSON の 30%。xarray で直接スライスでき、値の持ち方
  （×10 の int16・欠測 -32768）も store/observations.nc と揃う。

なぜ半月単位か: Pages 無料プランの上限は 1 サイト 20,000 ファイル・1 ファイル
25 MiB。10 分値を個別ファイルのまま残すと 144 個/日で 4.5 か月で上限に達する。
半月バンドルは実測外挿で約 7 MB（上限の 28%）に収まり、ファイルは年 24 個。
生 JSON の滞留 3,744 個と合わせても 50 年で 5,000 個弱にしかならない。

取得作法: 1 スロット 1 リクエスト（144 回/日）。既に持っているスロットは
再取得しない。ミラーが参照されるほど気象庁への総アクセスはむしろ減る。

使い方:
    python fetch_amedas_mirror.py            # cron から 10 分毎
    python fetch_amedas_mirror.py --dry-run
"""
from __future__ import annotations

import calendar
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import netCDF4 as nc

from weatherlib import jma

BASE = Path(__file__).resolve().parent
OUT = BASE / "public_amedas"
MAP = OUT / "map"
MASTER = BASE / "master"

JST = ZoneInfo("Asia/Tokyo")
SLOT_MINUTES = 10
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
# 気象庁自身の保持期間に合わせる（取得しに行く窓）
WINDOW_DAYS = 10
# 1 回の実行で取りに行く最大スロット数（長時間停止後に一気に叩かないための上限）
MAX_FETCH_PER_RUN = 60
# Pages の 1 ファイル上限 25 MiB。ここを超えそうなら封入時に警告する
PAGES_FILE_LIMIT = 25 * 1024 * 1024
SIZE_WARN = int(PAGES_FILE_LIMIT * 0.8)

URL_LATEST_TIME = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"

# 収集 Worker が置いた R2 の公開ベース URL（例 https://amedas.time-j.net）。
# 設定すると取得元がここになり、気象庁への往復は Worker の 10 分に 1 回だけになる。
# 未設定なら従来どおり気象庁から直接取る。
R2_BASE = os.environ.get("AMEDAS_R2_BASE", "").rstrip("/")

# 欠測値は store/observations.nc と同じ規約（値 int16 / 品質 int8）
FILL = np.int16(-32768)
FILL_B = np.int8(-1)

# (map JSON のキー, 格納倍率, 型, 単位) — 倍率は値域が int16 に収まるよう選ぶ。
# visibility は m のままだと 50,000 で溢れるため 10 m 単位で格納する。
#
# 積雪は夏の map JSON には現れない（amedastable.json の elems 6 桁目に
# 積雪計 336 地点があり、冬季のみ値が入る）。夏のデータだけを見て表を作ると
# 冬に無言で捨てることになるため、**未知のキーは自動で拾う**ようにしてある
# （下の unknown_elements と _f4 の扱いを参照）。ここに載っているのは
# 「int16 で正しく畳める」と分かっているものだけ。
ELEMENTS = [
    ("temp",             10,   "i2", "degC"),
    ("humidity",          1,   "i2", "%"),
    ("pressure",         10,   "i2", "hPa"),
    ("normalPressure",   10,   "i2", "hPa"),
    ("precipitation10m", 10,   "i2", "mm"),
    ("precipitation1h",  10,   "i2", "mm"),
    ("precipitation3h",  10,   "i2", "mm"),
    ("precipitation24h", 10,   "i2", "mm"),
    ("sun10m",            1,   "i2", "min"),
    ("sun1h",            10,   "i2", "h"),
    ("wind",             10,   "i2", "m/s"),
    ("windDirection",     1,   "i1", "16-point"),
    ("visibility",        0.1, "i2", "m"),
    ("snow",              1,   "i2", "cm"),
    ("snow1h",            1,   "i2", "cm"),
    ("snow6h",            1,   "i2", "cm"),
    ("snow12h",           1,   "i2", "cm"),
    ("snow24h",           1,   "i2", "cm"),
    # 毎正時のスロットにだけ入る（そのため正時のファイルは約 100KB 大きい）。
    # 非正時のファイル 1 本だけを見て表を作ると取りこぼす
    ("weather",           1,   "i2", "JMA weather code"),
]
KNOWN_KEYS = {k for k, _, _, _ in ELEMENTS}

SOURCE = "気象庁ホームページ (https://www.jma.go.jp/)"
# 生ペイロードの素通しは複製、NetCDF は再構成なので加工表記が要る
NOTICE_RAW = f"出典: {SOURCE}"
NOTICE_DERIVED = (f"出典: {SOURCE} / "
                  "気象庁のデータを編集・加工したものです。編集・加工の責任は AIseed にあります。")


def log(msg: str) -> None:
    print(f"[amedas-mirror] {msg}", flush=True)


def slot_name(ts: datetime) -> str:
    return ts.strftime("%Y%m%d%H%M00")


def slot_date(name: str) -> date:
    return date(int(name[:4]), int(name[4:6]), int(name[6:8]))


# ---------------------------------------------------------------- 半月の区切り

def period_of(d: date) -> tuple[int, int, int]:
    return (d.year, d.month, 1 if d.day <= 15 else 2)


def period_start(p: tuple[int, int, int]) -> date:
    y, m, half = p
    return date(y, m, 1) if half == 1 else date(y, m, 16)


def period_end(p: tuple[int, int, int]) -> date:
    y, m, half = p
    return date(y, m, 15) if half == 1 else date(y, m, calendar.monthrange(y, m)[1])


def period_path(p: tuple[int, int, int]) -> Path:
    y, m, half = p
    return OUT / "archive" / f"{y}" / f"{m:02d}-{half}.nc"


def period_label(p: tuple[int, int, int]) -> str:
    return f"{p[0]}-{p[1]:02d} {'前半' if p[2] == 1 else '後半'}"


# ---------------------------------------------------------------- 取得

def latest_slot() -> datetime:
    raw = jma.http_get(URL_LATEST_TIME).decode("ascii").strip()
    ts = datetime.fromisoformat(raw).astimezone(JST).replace(tzinfo=None)
    return ts.replace(minute=ts.minute // SLOT_MINUTES * SLOT_MINUTES, second=0, microsecond=0)


def wanted_slots(latest: datetime) -> list[datetime]:
    """窓の中で本来持っているべきスロットを新しい順に返す。"""
    oldest = latest - timedelta(days=WINDOW_DAYS)
    out, ts = [], latest
    while ts > oldest:
        out.append(ts)
        ts -= timedelta(minutes=SLOT_MINUTES)
    return out


def fetch_slot(name: str) -> tuple[bytes | None, str]:
    """1 スロットを取る。R2（Worker が収集済み）を先に見て、無ければ気象庁へ。

    R2 を優先するのは、収集を Worker に寄せて気象庁への往復を 1 系統に保つため。
    気象庁へ退避するのは、Worker が黙って止まっていた場合に**取り返しのつかない
    欠測**になるのを防ぐため（10 分値は 10 日で消える）。
    戻り値は (本文, 取得元)。取れなければ (None, 理由)。
    """
    if R2_BASE:
        try:
            # retry=0 が要る。R2 の 404 は「Worker がまだ集めていない」という
            # 正常な状態で、既定のリトライだと 1 件ごとに 2 秒待たされる
            return jma.http_get(f"{R2_BASE}/map/{name}.json", retry=0), "R2"
        except Exception:
            pass                                        # 未収集スロット → 気象庁で拾う
    try:
        return jma.http_get(jma.URL_AMEDAS_MAP.format(ts=name)), "JMA"
    except Exception as exc:                            # 欠番スロットは正常に起こりうる
        return None, str(exc)


def fetch_missing(latest: datetime, dry: bool) -> tuple[int, int]:
    """未取得スロットを新しい順に埋める。取り逃しは次回以降の実行で自然に回復する。"""
    MAP.mkdir(parents=True, exist_ok=True)

    missing = [ts for ts in wanted_slots(latest)
               if not period_path(period_of(ts.date())).exists()   # 封入済みは取り直さない
               and not (MAP / f"{slot_name(ts)}.json").exists()]
    if len(missing) > MAX_FETCH_PER_RUN:
        log(f"未取得 {len(missing)} スロットのうち新しい {MAX_FETCH_PER_RUN} 件のみ取得（残りは次回）")
        missing = missing[:MAX_FETCH_PER_RUN]
    if dry:
        log(f"[dry-run] 取得対象 {len(missing)} スロット"
            + (f"（取得元 {R2_BASE}）" if R2_BASE else "（取得元 気象庁）"))
        return 0, 0

    got = from_jma = 0
    for ts in missing:
        name = slot_name(ts)
        payload, origin = fetch_slot(name)
        if payload is None:
            log(f"  {name}: 取得できず（{origin}）")
            continue
        try:
            json.loads(payload)                        # 壊れた応答を配信しない
        except ValueError:
            log(f"  {name}: JSON として不正なため破棄")
            continue
        (MAP / f"{name}.json").write_bytes(payload)
        got += 1
        from_jma += origin == "JMA"
    return got, from_jma


# ---------------------------------------------------------------- 封入

def write_period_netcdf(p: tuple[int, int, int], paths: list[Path], out: Path) -> None:
    """半月分の生 JSON を NetCDF-4 へ封入する。チャンクは 1 日単位（ランダムアクセス優先）。

    生 JSON を全部メモリに載せると 2,160 スロットで数 GB になるため、
    地点集合を先に 1 周して集めてから、2 周目で配列へ流し込む。
    """
    names = sorted(q.stem for q in paths)
    by_name = {q.stem: q for q in paths}

    found: set[str] = set()
    seen_keys: set[str] = set()
    for name in names:
        try:
            snap = json.loads(by_name[name].read_bytes())
        except ValueError:
            log(f"  {name}: 読めないため除外")
            continue
        found.update(snap)
        for entry in snap.values():
            seen_keys.update(entry)
    stations = sorted(found)
    sidx = {s: i for i, s in enumerate(stations)}
    n_s, n_t = len(stations), len(names)

    # 表に無いキーも必ず残す。倍率が分からないので float32 で素通しし、警告を出す。
    # ここで捨てると気づいたときには生 JSON が消えていて回復できない。
    unknown = sorted(seen_keys - KNOWN_KEYS)
    if unknown:
        log(f"  警告: 未知の要素 {unknown} を float32 で保存（ELEMENTS に倍率を追加すべき）")
    spec = [(k, s, t, u) for k, s, t, u in ELEMENTS if k in seen_keys]
    spec += [(k, 1, "f4", "unknown") for k in unknown]

    start = period_start(p)
    val: dict[str, np.ndarray] = {}
    qual: dict[str, np.ndarray] = {}
    for key, _scale, typ, _unit in spec:
        if typ == "f4":
            val[key] = np.full((n_s, n_t), np.nan, dtype=np.float32)
        else:
            i1 = typ == "i1"
            val[key] = np.full((n_s, n_t), FILL_B if i1 else FILL,
                               dtype=np.int8 if i1 else np.int16)
        qual[key] = np.full((n_s, n_t), FILL_B, dtype=np.int8)

    for j, name in enumerate(names):
        try:
            snap = json.loads(by_name[name].read_bytes())
        except ValueError:
            continue
        for s, entry in snap.items():
            i = sidx[s]
            for key, scale, typ, _unit in spec:
                e = entry.get(key)
                if isinstance(e, list) and len(e) >= 2 and e[0] is not None:
                    val[key][i, j] = (float(e[0]) if typ == "f4"
                                      else int(round(float(e[0]) * scale)))
                    # snow1h 系は品質フラグが null で来ることがある（実測で
                    # 45,600 件）。欠測扱いにして値だけ残す
                    if e[1] is not None:
                        qual[key][i, j] = e[1]

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    ds = nc.Dataset(tmp, "w", format="NETCDF4")
    ds.createDimension("station", n_s)
    ds.createDimension("time", n_t)

    ds.title = (f"JMA AMeDAS 10-minute observations "
                f"{start.isoformat()}..{period_end(p).isoformat()}")
    ds.source = SOURCE
    ds.attribution = NOTICE_DERIVED
    ds.source_url = jma.URL_AMEDAS_MAP.format(ts="{ts}")
    ds.date_start = start.isoformat()
    ds.date_end = period_end(p).isoformat()
    ds.timezone = "Asia/Tokyo (JST, UTC+09:00)"
    ds.slot_minutes = SLOT_MINUTES
    ds.generated_at = datetime.now(JST).isoformat(timespec="seconds")
    ds.generated_by = "AIseed Weather / fetch_amedas_mirror.py"

    ds.createVariable("station_id", str, ("station",))[:] = np.array(stations)
    v_time = ds.createVariable("time", "i4", ("time",))
    v_time.units = f"minutes since {start.isoformat()}T00:00:00+09:00"
    v_time[:] = np.array(
        [(slot_date(n) - start).days * 1440 + int(n[8:10]) * 60 + int(n[10:12])
         for n in names], dtype=np.int32)

    chunk = (n_s, min(SLOTS_PER_DAY, n_t))     # 1 日 1 チャンク: 1 日読むのに全体を展開しない
    for key, scale, typ, unit in spec:
        f4 = typ == "f4"
        fill = np.nan if f4 else (FILL_B if typ == "i1" else FILL)
        var = ds.createVariable(key, typ, ("station", "time"), zlib=True, complevel=9,
                                shuffle=True, chunksizes=chunk, fill_value=fill)
        if not f4:
            # scale_factor 付きの変数へ代入すると netCDF4 が書き込み時にも値÷scale_factor を
            # 適用してしまい、こちらで整数化済みの値が再度 ×scale されて int16 を溢れる
            # (気圧 10037 → 100370 → 折り返して -30702)。素通しで格納する。
            var.set_auto_scale(False)
            var.scale_factor = 1.0 / scale
        var.units = unit
        var[:, :] = val[key]
        qv = ds.createVariable(f"{key}_q", "i1", ("station", "time"), zlib=True, complevel=9,
                               shuffle=True, chunksizes=chunk, fill_value=FILL_B)
        qv.long_name = f"JMA quality flag for {key} (0 = normal)"
        qv[:, :] = qual[key]
    ds.close()
    tmp.replace(out)


def seal_periods(latest: datetime) -> int:
    """10 日窓から完全に外れた半月を封入し、その期間の生 JSON を公開ツリーから外す。"""
    horizon = (latest - timedelta(days=WINDOW_DAYS)).date()

    by_period: dict[tuple[int, int, int], list[Path]] = {}
    for p in MAP.glob("*.json"):
        by_period.setdefault(period_of(slot_date(p.stem)), []).append(p)

    done = 0
    for period, paths in sorted(by_period.items()):
        if period_end(period) >= horizon:
            continue                        # まだ取得対象の日が残っている期間は触らない
        out = period_path(period)
        write_period_netcdf(period, paths, out)
        size = out.stat().st_size
        days = len({slot_date(q.stem) for q in paths})
        log(f"  {period_label(period)}: {len(paths)} スロット / {days} 日 → "
            f"{out.relative_to(OUT)}（{size/1024/1024:.1f} MB）")
        if size > SIZE_WARN:
            log(f"    警告: Pages の 1 ファイル上限 25 MiB に接近している（{size/1024/1024:.1f} MB）")
        for q in paths:
            q.unlink()
        done += 1
    return done


# ---------------------------------------------------------------- 公開メタデータ

def write_station_index() -> None:
    """地点索引を公開ツリーへ置く（Worker と消費側が読む地点マスタ）。

    正本は tgsvr の master/stations.json。Worker はこれを R2 から読んで
    担当地点を決めるので、**索引が先に無いと親 Worker は動けない**。

    並びは府県予報区（prec_no）順にする。親が地点を配る単位が地理的に
    まとまり、障害時にどの地域が欠けたか分かる。

    鍵はアメダス番号（10 分値の一次データがその体系）。統計 code は stat に
    併記し、observations.nc・平年値との突合はそちらで行う。
    """
    src = MASTER / "stations.json"
    if not src.exists():
        log("master/stations.json が無いため地点索引を作れない（build_master.py 未実行?）")
        return
    st = json.loads(src.read_text(encoding="utf-8"))["stations"]
    ordered = sorted(st.items(),
                     key=lambda kv: ((kv[1].get("etrn") or {}).get("prec_no", 99),
                                     kv[1]["amedas"]))
    body = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "attribution": NOTICE_DERIVED,
        "note": ("鍵はアメダス番号。統計 code は stat（observations.nc・平年値・"
                 "etrn はこちらの体系）。アメダス番号は移転や改番で変わりうるため、"
                 "長期の統計は stat を使うこと。"),
        "count": len(ordered),
        "stations": {
            r["amedas"]: {
                "stat": code, "intl": r["intl"], "name": r["name"],
                "kana": r.get("kana"), "pref": r.get("pref") or "",
                "lat": r["lat"], "lon": r["lon"], "alt": r["alt"],
                "type": r.get("type"), "elements": r.get("elements"),
                "prec_no": (r.get("etrn") or {}).get("prec_no"),
            } for code, r in ordered
        },
    }
    out = OUT / "station" / "index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    log(f"station/index.json ({out.stat().st_size/1024:.0f} KB / {len(ordered)} 地点・府県順)")


def write_index(latest: datetime) -> None:
    slots = sorted(p.stem for p in MAP.glob("*.json"))
    archives = []
    for p in sorted((OUT / "archive").glob("*/*.nc")):
        ds = nc.Dataset(p)
        archives.append({"path": str(p.relative_to(OUT)),
                         "start": ds.date_start, "end": ds.date_end,
                         "slots": ds.dimensions["time"].size,
                         "bytes": p.stat().st_size})
        ds.close()
    index = {
        "dataset": "JMA AMeDAS 10-minute observations (mirror + archive)",
        "attribution": NOTICE_RAW,
        "attribution_archive": NOTICE_DERIVED,
        "source_url": jma.URL_AMEDAS_MAP.format(ts="{ts}"),
        "license": "気象庁ホームページ利用規約に基づく利用（出典明示）",
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "latest_slot": slot_name(latest),
        "slot_minutes": SLOT_MINUTES,
        "fetch_window_days": WINDOW_DAYS,
        "elements": [k for k, _, _, _ in ELEMENTS],
        "map_slots": len(slots),
        "map_oldest": slots[0] if slots else None,
        "map_newest": slots[-1] if slots else None,
        "archive_periods": len(archives),
        "archive": archives,
        "note": ("map/ は気象庁の生ペイロードをそのまま複製したもの。半月分を "
                 "archive/ へ封入した時点で消えるため、常に 10〜26 日分が載る"
                 f"（取得窓は直近 {WINDOW_DAYS} 日）。archive/ は半月分を NetCDF-4 へ"
                 "再構成した加工データ（値は scale_factor つき int16、欠測 -32768、"
                 "品質は *_q 変数）。"),
    }
    (OUT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def write_headers() -> None:
    # 10 分更新なので latest と index は短命、確定済みの map/archive は不変として扱う
    (OUT / "_headers").write_text(
        "/*\n"
        "  Access-Control-Allow-Origin: *\n"
        "/latest.json\n"
        "  Cache-Control: public, max-age=300\n"
        "/index.json\n"
        "  Cache-Control: public, max-age=300\n"
        "/map/*\n"
        "  Cache-Control: public, max-age=31536000, immutable\n"
        "/archive/*\n"
        "  Cache-Control: public, max-age=31536000, immutable\n"
        "  Content-Type: application/x-netcdf\n",
        encoding="utf-8")


def main() -> int:
    dry = "--dry-run" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)

    try:
        latest = latest_slot()
    except Exception as exc:
        log(f"latest_time.txt を取得できないため中止（既存の公開ツリーは変更しない）: {exc}")
        return 1
    log(f"最新スロット: {slot_name(latest)}")

    got, from_jma = fetch_missing(latest, dry)
    if dry:
        return 0

    newest = MAP / f"{slot_name(latest)}.json"
    if newest.exists():
        shutil.copyfile(newest, OUT / "latest.json")
        # 気象庁と同じ latest_time.txt も置く。これがあると消費側は
        # ベース URL を差し替えるだけでよく、取得コードを変えずに済む
        (OUT / "latest_time.txt").write_text(
            latest.replace(tzinfo=JST).isoformat(timespec="seconds"), encoding="utf-8")

    sealed = seal_periods(latest)
    write_station_index()
    write_index(latest)
    write_headers()

    slots = len(list(MAP.glob("*.json")))
    periods = len(list((OUT / "archive").glob("*/*.nc")))
    # 気象庁へ退避した件数は目に見えるようにする。恒常的に出るなら
    # 収集 Worker が止まっている合図（黙って欠測させないため）
    log(f"取得 {got} スロット"
        + (f"（うち気象庁へ退避 {from_jma}）" if from_jma else "")
        + f" / 公開中の生 JSON {slots} / アーカイブ {periods} 期間"
        + (f" / 新規封入 {sealed} 期間" if sealed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
