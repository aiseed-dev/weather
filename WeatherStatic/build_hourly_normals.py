#!/usr/bin/env python3
"""時別の平年値（気温）を作る。気象庁は時別の平年値を公表していないので自前で作る。

なぜ要るか
    トップページで「いまの 10 分値の気温」と「この日のこの時刻の平年」を比べたい。
    気象庁の平年値（1991〜2020）は日別・半旬・旬・月・年しかない。

方式（hybrid: 日別平年値 + サンプル日の時別値から推定した日変化の形）
    平年(時刻 h, 日 d) = 日平均気温の平年(d) + 日変化の偏差(h, d)

    日変化の偏差 = 「その日の時別値 − その日の 24 時間平均」を標本日で平均したもの。
      * 標本日は etrn の時別値ページ（hourly_s1.php）から取る。1 ページ = 1 地点 1 日。
      * 日をまたいで平滑化する。標本日の暦日（うるう年基準の通日）からの距離で
        ガウス重み（既定 σ=10 日）をかけた加重平均。月の境目で段差が出ない。
      * 振幅は、その日の平年の日較差（最高−最低の平年）と、重みづけした標本日の
        平年日較差の比で補正する（±30% まで）。月内で日較差が増減する分を拾う。
    偏差は各標本日で平均 0 なので、24 値の平均は日平均気温の平年と一致する
    （整数丸めの誤差は最大剰余法で配り、×10 整数の合計まで厳密に一致させる）。

    重要な性質: **時別の平年の最高は、日最高気温の平年より低い**（逆に最低は高い）。
    日最高・最低は連続観測の極値で、時別値（毎正時の瞬時値）は取りこぼす。
    また日ごとに最高の起こる時刻がずれるので、平均すると山が丸まる。これは正しい
    挙動で、min-max 方式（日最高・最低に山と谷をぴったり合わせる）は「この時刻の平年」
    としては過大な振幅になる。だから平均アンカー方式を採る。

時刻の約束（出力 JSON の "hours" にも書く）
    気象庁 etrn の気温は「毎正時の瞬時値」。ページの「時」は 1〜24 で、
    N 時 = 当日 N:00 JST の値、24 時 = 翌日 0:00。官署の日平均気温はこの 24 値の
    平均（東京 2019-07-03 で時別 24 値の平均 24.93 = 日別ページ 24.9 と確認）。
    出力の配列 index i (0..23) = 気象庁の「i+1 時」= 当日 (i+1):00 JST。
    index 23 は翌日 0:00 の値。当日 0:00〜1:00 の間を補間したいときは、
    0:00 の値として「前日の index 23」を使う（normal_at() 参照）。
    降水量・日照のような「前 1 時間の積算」ではないので、半時間ずらす必要はない。

使い方
    python build_hourly_normals.py plan   [--plan sample|full] [...]   # 件数と所要時間（通信なし）
    python build_hourly_normals.py fetch  --window 23:00-06:00 [...]   # ページを取ってキャッシュ（通信）
    python build_hourly_normals.py build  [...]                        # キャッシュから平年を計算
    python build_hourly_normals.py show   --stations 47662 --date 07-03

    既定の対象は HOME_CITIES の 10 都市（--stations で変更。"main" で官署 57 地点）。
    fetch は何度でも再開できる（取得済みページはスキップ）。build は途中のデータでも
    動き、被覆率を表示する。

    標本計画 (--plan)
      sample（既定）: 1992,1995,...,2019 の 10 年から、月ごとに --days-per-month 日
                      （既定 90）を、日付が月内で均等に散るよう抽出（再現可能）。
      full        : 1991〜2020 の全日（地点あたり約 1 万 949 ページ）。
      どちらも「全日の部分集合」なので、キャッシュは共有される。sample を先に回して
      build し、足りなければ full で続きを取れる。取得順はシャッフル済みで、途中まで
      でも偏りのない標本になる。

    日別平年値の入手 (--normals)
      master（既定）: master/normals/{code}.json（平年値 ZIP 由来）。本番用。
      etrn          : 平年値ページ nml_sfc_d.php を月ごとに取ってキャッシュ（地点 × 月、
                      12 ページ/地点）。手元の master が作り物のとき用。

    気象庁への配慮
      1 リクエストずつ・間隔既定 1.5 秒（1.0 秒未満は不可）・User-Agent は
      weatherlib.jma と同じ（連絡先入り）。429/5xx は数分〜1 時間のバックオフ。
      --window で通信してよい時間帯（JST）を限る。窓の外は待機。
      --max-requests でキャッシュ dir の累計リクエスト数（_requests.log の行数）に
      上限をかけられる。
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import math
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from weatherlib import etrn_worker, jma
from weatherlib.hourly_normals import normal_at  # noqa: F401  利用側の参照実装
from weatherlib.stations import BY_CODE, MAIN_STATIONS

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MASTER = BASE / "master"
DEFAULT_CACHE = DATA / "etrn_hourly"          # .gitignore 済み（WeatherStatic/data/）
DEFAULT_OUT = MASTER / "normals_hourly"       # .gitignore 済み（WeatherStatic/master/）

# generate.py の HOME_CITIES と同じ（generate.py は import が重いので写している。
# 札幌, 仙台, 東京, 名古屋, 大阪, 広島, 高松, 福岡, 鹿児島, 那覇）
HOME_CITIES = [47412, 47590, 47662, 47636, 47772, 47765, 47891, 47807, 47827, 47936]

USER_AGENT = jma.USER_AGENT + " build_hourly_normals"
TIMEOUT = jma.TIMEOUT
JST = timezone(timedelta(hours=9))

URL_HOURLY = ("https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php"
              "?prec_no={prec}&block_no={block}&year={y}&month={m}&day={d}&view=")
URL_NORMALS_D = jma.URL_NORMALS            # nml_sfc_d.php（日別平年値、月ごと）

NORMAL_YEARS = (1991, 2020)
DEFAULT_SAMPLE_YEARS = list(range(1992, 2020, 3))   # 1992,1995,...,2019（10 年。重心 2005.5）
DEFAULT_DAYS_PER_MONTH = 90
DEFAULT_SIGMA = 10.0                      # 日をまたぐ平滑化（ガウス σ, 日）
DEFAULT_INTERVAL = 1.5
MIN_INTERVAL = 1.0
LEAP = 2000                               # 通日・暦日の基準（2/29 を含む年）
METHOD = "diurnal-deviation-anchored-v1"


def log(msg: str) -> None:
    print(f"[{datetime.now(JST):%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 暦

def dim(month: int, year: int = LEAP) -> int:
    return (date(year + (month == 12), month % 12 + 1, 1) - date(year, month, 1)).days


def doy(month: int, day: int) -> int:
    """うるう年基準の通日 0..365。"""
    return date(LEAP, month, day).timetuple().tm_yday - 1


# ---------------------------------------------------------------- 地点

def resolve_stations(spec: str) -> list[int]:
    if spec in ("home", ""):
        return list(HOME_CITIES)
    if spec == "main":
        return [s["code"] for s in MAIN_STATIONS]
    codes = [int(x) for x in spec.split(",") if x.strip()]
    for c in codes:
        if c not in BY_CODE:
            raise SystemExit(f"主要 57 都市にない地点: {c}")
    return codes


# ---------------------------------------------------------------- 取得計画

@dataclass(frozen=True)
class Task:
    kind: str          # "h" 時別値ページ / "n" 日別平年値ページ
    code: int
    key: str           # ログ用（20190703 / 07）
    url: str
    path: Path


def hourly_path(cache: Path, code: int, d: date) -> Path:
    return cache / "hourly" / str(code) / f"{code}_{d:%Y%m%d}.html.gz"


def normals_path(cache: Path, code: int, month: int) -> Path:
    return cache / "normals_d" / f"{code}_{month:02d}.html.gz"


def hourly_task(cache: Path, code: int, d: date) -> Task:
    st = BY_CODE[code]
    return Task("h", code, f"{d:%Y%m%d}",
                URL_HOURLY.format(prec=st["prec_no"], block=code, y=d.year, m=d.month, d=d.day),
                hourly_path(cache, code, d))


def sample_dates(code: int, month: int, years: list[int], n: int, seed: str) -> list[date]:
    """1 地点 × 1 月の標本日。月内の日付を層化（均等）に散らし、年は均等に割り当てる。

    並びはシャッフル済み（途中までの取得でも偏らない）。(code, month, seed) で再現可能。
    2/29 は標本にしない（うるう年の 2/29 の平年は前後の日から平滑化で出る）。
    """
    rng = random.Random(f"{seed}-{code}-{month}")
    nd = 28 if month == 2 else dim(month)
    ys = [years[i % len(years)] for i in range(n)]
    rng.shuffle(ys)
    seen: set[tuple[int, int]] = set()
    out: list[date] = []
    for k in range(n):
        d0 = 1 + int((k + 0.5) / n * nd)
        d1 = d0
        for off in (0, 1, -1, 2, -2, 3, -3):          # 同じ (年, 日) が重なったらずらす
            d1 = min(max(d0 + off, 1), nd)
            if (ys[k], d1) not in seen:
                break
        if (ys[k], d1) in seen:
            continue
        seen.add((ys[k], d1))
        out.append(date(ys[k], month, d1))
    rng.shuffle(out)
    return out


def make_tasks(args, hourly_only: bool = False) -> list[Task]:
    """取得すべきページの一覧（取得済みも含む）。順序が取得順。"""
    cache = Path(args.cache_dir)
    codes = resolve_stations(args.stations)
    months = parse_months(args.months)
    tasks: list[Task] = []
    if args.normals == "etrn" and not hourly_only:
        for code in codes:
            st = BY_CODE[code]
            for m in months:
                tasks.append(Task("n", code, f"{m:02d}",
                                  URL_NORMALS_D.format(prec_no=st["prec_no"], block_no=code, month=m),
                                  normals_path(cache, code, m)))
    hourly: list[tuple[int, int, date]] = []   # (round, code順, date) 並べ替え用
    if args.plan == "full":
        y0, y1 = NORMAL_YEARS
        alld = []
        d = date(y0, 1, 1)
        while d <= date(y1, 12, 31):
            if d.month in months:
                alld.append(d)
            d += timedelta(days=1)
        items = [(code, d) for code in codes for d in alld]
        random.Random(args.seed).shuffle(items)
        tasks += [hourly_task(cache, c, d) for c, d in items]
    else:
        years = [int(y) for y in args.years.split(",")] if args.years else DEFAULT_SAMPLE_YEARS
        order = {c: i for i, c in enumerate(codes)}
        for code in codes:
            for m in months:
                for k, d in enumerate(sample_dates(code, m, years, args.days_per_month, args.seed)):
                    hourly.append((k, order[code] * 100 + m, d))
        # 各 (地点, 月) の k 番目を一巡してから次の周回へ → 途中でも全地点・全月が薄く揃う
        hourly.sort(key=lambda x: (x[0], x[1]))
        tasks += [hourly_task(cache, codes[x[1] // 100], x[2]) for x in hourly]
    return tasks


def parse_months(s: str | None) -> list[int]:
    if not s:
        return list(range(1, 13))
    ms = sorted({int(x) for x in s.split(",")})
    if any(m < 1 or m > 12 for m in ms):
        raise SystemExit("--months は 1〜12")
    return ms


# ---------------------------------------------------------------- 解析（キャッシュ済み HTML → 値）

def _cells(tr: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]


def _temp_cell(s: str) -> int | None:
    """気温セル → ×10 整数。欠測・資料不足（` ]`）・非数値は None。` )`（準正常）は採る。"""
    s = s.strip().replace("　", "")
    if s.endswith(")"):
        s = s[:-1].strip()
    elif s.endswith("]"):
        return None
    try:
        v = int(round(float(s) * 10))
    except ValueError:
        return None
    return v if -500 <= v <= 500 else None


# hourly_s1 の本文行は 17 セル: [時, 現地気圧, 海面気圧, 降水量, 気温, 露点, ...]
# （見出しは rowspan で崩れて見えるが、本文行の index 4 が気温。東京 2019-07-03 で検証）
HOURLY_TEMP_COL = 4


def parse_hourly(html: str) -> list[int | None] | None:
    """時別値ページ → 長さ 24 のリスト（index i = 気象庁の i+1 時、×10 整数 / 欠測 None）。

    表が壊れている（24 行そろわない）ときは None。ページ自体の妥当性判定にも使う。
    """
    out: dict[int, int | None] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        c = _cells(tr)
        if len(c) < 10 or not c[0].isdigit():
            continue
        h = int(c[0])
        if 1 <= h <= 24 and len(c) > HOURLY_TEMP_COL:
            out[h] = _temp_cell(c[HOURLY_TEMP_COL])
    if sorted(out) != list(range(1, 25)):
        return None
    return [out[h] for h in range(1, 25)]


def parse_normals_d(html: str) -> dict[int, tuple[int, int, int]]:
    """日別平年値ページ → {日: (tavg, tmax, tmin)}（×10 整数）。列は jma.fetch_daily_normals と同じ。"""
    res: dict[int, tuple[int, int, int]] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        c = _cells(tr)
        if not c or not re.fullmatch(r"\d+日", c[0] or "") or len(c) < 5:
            continue
        v = [jma._x10(c[i]) for i in (2, 3, 4)]
        if any(x == -999 for x in v):
            continue
        res[int(c[0].rstrip("日"))] = tuple(v)
    return res


def read_gz(p: Path) -> str:
    with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
        return f.read()


def valid_page(kind: str, html: str) -> bool:
    if kind == "h":
        return parse_hourly(html) is not None
    return len(parse_normals_d(html)) >= 28


# ---------------------------------------------------------------- 取得（fetch）

class Ledger:
    """cache_dir/_requests.log に 1 リクエスト 1 行を追記する。行数 = 気象庁への累計リクエスト数。"""

    def __init__(self, path: Path):
        self.path = path
        self.n = 0
        if path.exists():
            with open(path, "rb") as f:
                self.n = sum(1 for _ in f)

    def add(self, kind: str, key: str, status) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(JST).isoformat(timespec='seconds')}\t{kind}\t{key}\t{status}\n")
        self.n += 1


def parse_window(s: str | None):
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", s)
    if not m:
        raise SystemExit("--window は HH:MM-HH:MM（JST）")
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    return a, b


def window_hours(win) -> float:
    if win is None:
        return 24.0
    a, b = win
    return ((b - a) % 1440 or 1440) / 60


def in_window(win, now: datetime) -> bool:
    if win is None:
        return True
    a, b = win
    t = now.hour * 60 + now.minute
    return (a <= t < b) if a < b else (t >= a or t < b)


def seconds_until_open(win, now: datetime) -> float:
    a, _ = win
    t = now.hour * 60 + now.minute + now.second / 60
    return ((a - t) % 1440) * 60


class Stop:
    flag = False


def _on_signal(signum, frame):
    Stop.flag = True
    log(f"シグナル {signum} を受信。いまの 1 件が済んだら止まります（取得済みページは安全）")


def sleep_interruptible(sec: float) -> None:
    end = time.monotonic() + sec
    while not Stop.flag:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 20.0))


def http_fetch(url: str) -> tuple[int, bytes | None]:
    """1 回だけ GET（暗黙のリトライなし）。(HTTP ステータス, 本文)。通信失敗は status=0。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None




def load_worker_env() -> tuple[str, str]:
    """Worker の URL と合言葉（weatherlib/etrn_worker.py。値は表示しない）。"""
    return etrn_worker.load_env()


def worker_fetch(t: "Task", base: str, token: str) -> tuple[int, bytes | None]:
    """Cloudflare の Worker（POST /etrn）経由で取る。気象庁へは Cloudflare から行き、
    deb2 の定常の取得（実況・予報）が気象庁に止められる心配をなくす。
    Worker は R2 の etrn/{key} にも置く。戻り値は気象庁の HTTP ステータスと本文。"""
    key = f"{'hourly' if t.kind == 'h' else 'normals_d'}/{t.code}/{t.path.name.removesuffix('.gz')}"
    return etrn_worker.fetch(t.url, key, base, token, user_agent=USER_AGENT, timeout=TIMEOUT + 15)


def atomic_write_gz(path: Path, body: bytes) -> None:
    """一時ファイルに書いて rename。kill されても半端なページが残らない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(body)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, path)


def fail_path(t: Task) -> Path:
    return t.path.with_name(t.path.name + ".fail")


def attempts(t: Task) -> int:
    try:
        return int(fail_path(t).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def is_done(t: Task, max_attempts: int) -> bool:
    return t.path.exists() or attempts(t) >= max_attempts


def cmd_fetch(args) -> int:
    interval = max(MIN_INTERVAL, args.interval)
    if args.interval < MIN_INTERVAL:
        log(f"--interval は {MIN_INTERVAL} 秒未満にできません。{MIN_INTERVAL} 秒で動かします")
    win = parse_window(args.window)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    lockf = open(cache / "_fetch.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("別の fetch が同じ cache dir で動いています。終了します")
        return 1

    for p in cache.rglob("*.tmp[0-9]*"):          # 前回 kill された書きかけの残骸
        p.unlink(missing_ok=True)

    ledger = Ledger(cache / "_requests.log")
    via = load_worker_env() if args.via_worker else None
    tasks = make_tasks(args)
    total = len(tasks)
    pending = [t for t in tasks if not is_done(t, args.max_attempts)]
    done0 = total - len(pending)
    log(f"開始: 全 {total:,} ページ / 取得済み {done0:,} / 残り {len(pending):,}"
        f"（間隔 {interval} 秒、窓 {args.window or 'なし'}、累計リクエスト {ledger.n:,}"
        f"{'、Cloudflare の Worker 経由' if via else ''}）")
    if not pending:
        log("すべて取得済みです")
        return 0

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    t_start = time.monotonic()
    idle = 0.0                  # 窓待ち・バックオフで止まっていた秒数（レート計算から除く）
    last_req = 0.0
    n_req = n_ok = n_fail = n_giveup = 0
    consec_fail = consec_throttle = 0
    pass_backoff = 600.0
    announced_closed = False
    remaining = len(pending)

    def progress() -> None:
        active = max(time.monotonic() - t_start - idle, 1e-6)
        rate = n_ok / active * 3600                     # 取得成功ページ/時
        done = total - remaining
        if rate > 0:
            eta_h = remaining / rate
            nights = (f"、窓 {window_hours(win):.0f} 時間/晩なら約 {math.ceil(eta_h / window_hours(win))} 晩"
                      if win else "")
            eta = f"残り約 {eta_h:.1f} 時間（稼働時間）{nights}"
        else:
            eta = "残り時間は未算出"
        log(f"進捗 {done:,}/{total:,} ({done / total * 100:.1f}%) | 本回 リクエスト {n_req:,} "
            f"成功 {n_ok:,} 失敗 {n_fail} 断念 {n_giveup} | {rate:,.0f} ページ/時 | {eta}")

    try:
        while pending and not Stop.flag:
            retry: list[Task] = []
            for t in pending:
                if Stop.flag:
                    retry.append(t)
                    continue
                # 通信してよい時間帯まで待つ
                while not Stop.flag and not in_window(win, datetime.now(JST)):
                    wait = seconds_until_open(win, datetime.now(JST))
                    if not announced_closed:
                        log(f"窓の外です。{wait / 3600:.1f} 時間後（{args.window} の開始）に再開します")
                        announced_closed = True
                    w0 = time.monotonic()
                    sleep_interruptible(min(wait, 300))
                    idle += time.monotonic() - w0
                if Stop.flag:
                    retry.append(t)
                    continue
                if announced_closed:
                    log("窓が開きました。再開します")
                    announced_closed = False
                if args.max_requests and ledger.n >= args.max_requests:
                    log(f"--max-requests {args.max_requests} に到達（累計 {ledger.n}）。止まります")
                    Stop.flag = True
                    retry.append(t)
                    continue
                if args.limit and n_req >= args.limit:
                    log(f"--limit {args.limit} に到達。止まります（次回は続きから）")
                    Stop.flag = True
                    retry.append(t)
                    continue

                # 前回の開始から interval 秒あける
                gap = last_req + interval - time.monotonic()
                if gap > 0:
                    time.sleep(gap)
                last_req = time.monotonic()
                status, body = (worker_fetch(t, *via) if via else http_fetch(t.url))
                ledger.add(t.kind, f"{t.code}-{t.key}", status)
                n_req += 1

                if status == 200 and body is not None and valid_page(
                        t.kind, body.decode("utf-8", errors="replace")):
                    atomic_write_gz(t.path, body)
                    fail_path(t).unlink(missing_ok=True)
                    n_ok += 1
                    remaining -= 1
                    consec_fail = consec_throttle = 0
                elif status == 429 or status >= 500 or status == 0:
                    # サーバー都合（過負荷・制限・通信断）。この件の失敗とは数えず、長めに待つ
                    consec_throttle += 1
                    n_fail += 1
                    wait = min(300.0 * 2 ** (consec_throttle - 1), 3600.0)
                    log(f"警告: HTTP {status} ({t.code} {t.key})。{wait / 60:.0f} 分待ちます"
                        f"（連続 {consec_throttle} 回）")
                    w0 = time.monotonic()
                    sleep_interruptible(wait)
                    idle += time.monotonic() - w0
                    retry.append(t)
                else:
                    # 404・表が壊れた 200 など。取れていないので保存せず、後の周回で再試行
                    n_fail += 1
                    consec_fail += 1
                    a = attempts(t) + 1
                    fail_path(t).write_text(str(a))
                    if a >= args.max_attempts:
                        n_giveup += 1
                        remaining -= 1
                        log(f"断念: {t.code} {t.key} HTTP {status}、{a} 回失敗")
                    else:
                        log(f"警告: {t.code} {t.key} HTTP {status}（{a}/{args.max_attempts} 回目）。後で再試行")
                        retry.append(t)
                    wait = min(30.0 * 2 ** (consec_fail - 1), 600.0)   # 連続失敗は指数で間をあける
                    w0 = time.monotonic()
                    sleep_interruptible(wait)
                    idle += time.monotonic() - w0

                if n_req % 100 == 0:
                    progress()

            pending = retry
            if pending and not Stop.flag:
                log(f"周回終了。再試行待ち {len(pending)} 件。{pass_backoff / 60:.0f} 分あけて再開")
                w0 = time.monotonic()
                sleep_interruptible(pass_backoff)
                idle += time.monotonic() - w0
                pass_backoff = min(pass_backoff * 2, 3600.0)
    finally:
        progress()
        lockf.close()
    if not pending:
        log("完了: すべてのページがキャッシュ済み（または断念）です。build に進めます")
    return 0


# ---------------------------------------------------------------- 日別平年値の読み込み

def load_daily_normals(code: int, args) -> dict[tuple[int, int], tuple[int, int, int]]:
    """{(月, 日): (tavg, tmax, tmin)}（×10 整数。うるう年の 2/29 を含む場合あり）。"""
    out: dict[tuple[int, int], tuple[int, int, int]] = {}
    if args.normals == "master":
        p = MASTER / "normals" / f"{code}.json"
        if not p.exists():
            raise SystemExit(f"{p} がありません（build_master.py --normals か --normals etrn）")
        daily = json.loads(p.read_text(encoding="utf-8"))["daily"]
        for ms, el in daily.items():
            for i in range(len(el["tavg"])):
                v = (el["tavg"][i], el["tmax"][i], el["tmin"][i])
                if None not in v:
                    out[(int(ms), i + 1)] = v
    else:
        for m in range(1, 13):
            p = normals_path(Path(args.cache_dir), code, m)
            if p.exists():
                for d, v in parse_normals_d(read_gz(p)).items():
                    out[(m, d)] = v
    return out


# ---------------------------------------------------------------- 計算（build）

def load_samples(cache: Path, code: int, months: list[int]):
    """キャッシュ済みの時別値ページ → [(date, np.array(24) ×10)]（24 値そろった日のみ）。"""
    out = []
    n_bad = 0
    for p in sorted((cache / "hourly" / str(code)).glob(f"{code}_*.html.gz")):
        ds = p.name.split("_")[1][:8]
        d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
        if d.month not in months:
            continue
        v = parse_hourly(read_gz(p))
        if v is None or None in v:
            n_bad += 1
            continue
        out.append((d, np.array(v, dtype=float)))
    return out, n_bad


def round_to_sum(vals: np.ndarray, target: int) -> list[int]:
    """最大剰余法で整数化し、合計を target に厳密に合わせる。"""
    fl = np.floor(vals).astype(int)
    rem = int(target - fl.sum())
    order = np.argsort(-(vals - fl))
    for i in order[:max(rem, 0)]:
        fl[i] += 1
    return [int(x) for x in fl]


def build_station(code: int, args) -> dict | None:
    cache = Path(args.cache_dir)
    months = parse_months(args.months)
    nml = load_daily_normals(code, args)
    samples, n_bad = load_samples(cache, code, months)
    if not samples:
        log(f"{code}: 標本なし（fetch を先に）")
        return None
    planned = defaultdict(int)
    for t in make_tasks(args, hourly_only=True):
        if t.code == code:
            planned[int(t.key[4:6])] += 1
    got = defaultdict(int)
    for d, _ in samples:
        got[d.month] += 1

    T = np.stack([v for _, v in samples])                        # (N, 24) ×10
    dev = T - T.mean(axis=1, keepdims=True)
    s_doy = np.array([doy(d.month, d.day) for d, _ in samples], dtype=float)
    rng_n = np.array([(nml[(d.month, d.day)][1] - nml[(d.month, d.day)][2])
                      if (d.month, d.day) in nml else np.nan for d, _ in samples])

    sigma = args.sigma
    days = [(m, dd) for m in months for dd in range(1, dim(m) + 1)]
    t_doy = np.array([doy(m, dd) for m, dd in days], dtype=float)
    delta = np.abs(t_doy[:, None] - s_doy[None, :])
    delta = np.minimum(delta, 366 - delta)                       # 通日の円環距離
    W = np.exp(-0.5 * (delta / sigma) ** 2) * (delta <= 3 * sigma)
    near = (delta <= 15).sum(axis=1)                             # ±15 日内の標本日数
    sw = W.sum(axis=1)
    ok = (near >= args.min_samples) & (sw > 0)
    sw_safe = np.where(sw > 0, sw, 1.0)
    S = (W @ dev) / sw_safe[:, None]                             # (days, 24) 偏差の加重平均
    var = np.maximum((W @ dev ** 2) / sw_safe[:, None] - S ** 2, 0)
    n_eff = sw ** 2 / np.maximum((W ** 2).sum(axis=1), 1e-12)
    se = np.sqrt(var) / np.sqrt(np.maximum(n_eff, 1))[:, None]   # ×10
    valid_r = ~np.isnan(rng_n)
    Rbar = (W[:, valid_r] @ rng_n[valid_r]) / np.maximum(W[:, valid_r].sum(axis=1), 1e-12)

    daily: dict[str, dict] = {}
    chk = defaultdict(lambda: defaultdict(list))
    se_by_m: dict[int, list] = defaultdict(list)
    for i, (m, dd) in enumerate(days):
        arr = daily.setdefault(str(m), {"temp": [None] * dim(m)})["temp"]
        if not ok[i] or (m, dd) not in nml:
            continue
        tavg, tmax, tmin = nml[(m, dd)]
        scale = 1.0
        if args.amp_scale and Rbar[i] > 0:
            scale = float(np.clip((tmax - tmin) / Rbar[i], 0.7, 1.3))
        vals = tavg + S[i] * scale
        r = round_to_sum(vals, 24 * tavg)
        arr[dd - 1] = r
        rr = np.array(r)
        chk[m]["mean_err"].append(abs(rr.mean() - tavg))
        chk[m]["max_gap"].append((rr.max() - tmax) / 10)
        chk[m]["min_gap"].append((rr.min() - tmin) / 10)
        chk[m]["hmax"].append(int(rr.argmax()) + 1)
        chk[m]["hmin"].append(int(rr.argmin()) + 1)
        chk[m]["range"].append((rr.max() - rr.min()) / 10)
        chk[m]["range_n"].append((tmax - tmin) / 10)
        se_by_m[m].append(se[i].max() / 10)
    daily = {m: v for m, v in daily.items() if any(x is not None for x in v["temp"])}

    check = {}
    for m, c in sorted(chk.items()):
        check[str(m)] = {
            "mean24_minus_tavg_max_abs_C": round(max(c["mean_err"]) / 10, 3),
            "hourly_max_minus_tmax_mean_C": round(float(np.mean(c["max_gap"])), 2),
            "hourly_min_minus_tmin_mean_C": round(float(np.mean(c["min_gap"])), 2),
            "hour_of_max_mode": int(np.bincount(c["hmax"]).argmax()),
            "hour_of_min_mode": int(np.bincount(c["hmin"]).argmax()),
            "hourly_range_mean_C": round(float(np.mean(c["range"])), 2),
            "tmax_minus_tmin_normal_mean_C": round(float(np.mean(c["range_n"])), 2),
            "se_max_over_hours_mean_C": round(float(np.mean(se_by_m[m])), 2),
        }
    return {
        "code": code,
        "name": BY_CODE[code]["name"],
        "method": METHOD,
        "method_note": ("平年(h,d) = 日別平年値の日平均気温(d) + 日変化の偏差(h,d)。偏差 = 標本日の"
                        "(時別値 − その日の 24 時間平均) を暦日方向にガウス平滑(σ=sigma_days)して加重平均、"
                        "日較差の平年で振幅補正。24 値の平均は日平均気温の平年と一致。時別の最高は"
                        "日最高気温の平年より低い（時別値は瞬時値で、最高の時刻が日ごとにずれるため）"),
        "period": "1991-2020",
        "period_note": ("日平均は気象庁 1991-2020 年平年値そのもの。日変化の形は"
                        "sample.years の標本日の時別値から推定（plan=full なら 1991-2020 の全日）"),
        "hours": ("気象庁 etrn の時別値の「時」(1..24)。配列 index i (0..23) = 「i+1 時」 = 当日 (i+1):00 JST の"
                  "毎正時の気温（瞬時値）。index 23 は翌日 0:00 の値。当日 0:00 の値は前日の index 23。"),
        "unit": "0.1 degC (integer)",
        "daily": daily,
        "daily_note": 'daily["月"]["temp"][日-1] = 24 値のリスト。標本が足りない日は null',
        "sample": {
            "plan": args.plan,
            "years": (list(range(NORMAL_YEARS[0], NORMAL_YEARS[1] + 1)) if args.plan == "full"
                      else ([int(y) for y in args.years.split(",")] if args.years
                            else DEFAULT_SAMPLE_YEARS)),
            "sigma_days": sigma,
            "amp_scale": bool(args.amp_scale),
            "pages_used": len(samples),
            "pages_unusable": n_bad,
            "by_month": {str(m): {"used": got.get(m, 0), "planned": planned.get(m, 0)}
                         for m in months},
        },
        "daily_normals_source": ("master/normals/{code}.json" if args.normals == "master"
                                 else "etrn nml_sfc_d.php（キャッシュ）"),
        "check": check,
        "built_at": datetime.now(JST).isoformat(timespec="seconds"),
        "attribution": "出典: 気象庁ホームページ（過去の気象データ・平年値）を加工して作成",
    }


def cmd_build(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for code in resolve_stations(args.stations):
        obj = build_station(code, args)
        if obj is None:
            rc = 1
            continue
        p = out_dir / f"{code}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(p)
        n_days = sum(1 for v in obj["daily"].values() for x in v["temp"] if x is not None)
        s = obj["sample"]
        planned = sum(v["planned"] for v in s["by_month"].values())
        log(f"{code} {obj['name']}: 標本 {s['pages_used']}/{planned} ページ（使えず {s['pages_unusable']}）"
            f" → {n_days} 日分を出力 {p}")
        print(f"  {'月':>3} {'標本':>5} {'24値平均-平年 最大':>16} {'時別最高-日最高平年':>18} "
              f"{'時別最低-日最低平年':>18} {'最高/最低の時':>12} {'時別較差/平年較差':>16} {'SE(最大)':>8}")
        for m, c in obj["check"].items():
            u = s["by_month"][m]["used"]
            print(f"  {m:>3} {u:>5} {c['mean24_minus_tavg_max_abs_C']:>14.3f}℃ "
                  f"{c['hourly_max_minus_tmax_mean_C']:>16.2f}℃ {c['hourly_min_minus_tmin_mean_C']:>16.2f}℃ "
                  f"{c['hour_of_max_mode']:>6}/{c['hour_of_min_mode']:<5} "
                  f"{c['hourly_range_mean_C']:>7.2f}/{c['tmax_minus_tmin_normal_mean_C']:<7.2f}℃ "
                  f"{c['se_max_over_hours_mean_C']:>7.2f}℃")
    return rc


# ---------------------------------------------------------------- 利用側のための参照実装

# normal_at() は weatherlib/hourly_normals.py にある（トップの地図と共用）


def cmd_show(args) -> int:
    code = resolve_stations(args.stations)[0]
    p = Path(args.out_dir) / f"{code}.json"
    obj = json.loads(p.read_text(encoding="utf-8"))
    m, d = (int(x) for x in args.date.split("-"))
    row = obj["daily"].get(str(m), {}).get("temp", [None] * 31)[d - 1]
    print(f"{code} {obj['name']} {m}/{d} 時別平年（℃, 1..24 時）")
    if row is None:
        print("  この日は標本不足で未出力")
        return 1
    print("  " + " ".join(f"{h + 1:>2}:{v / 10:.1f}" for h, v in enumerate(row)))
    print(f"  平均 {sum(row) / 240:.2f}  最高 {max(row) / 10:.1f}  最低 {min(row) / 10:.1f}")
    return 0


# ---------------------------------------------------------------- plan

def cmd_plan(args) -> int:
    tasks = make_tasks(args)
    cache = Path(args.cache_dir)
    n_h = sum(1 for t in tasks if t.kind == "h")
    n_n = sum(1 for t in tasks if t.kind == "n")
    cached = sum(1 for t in tasks if t.path.exists())
    win = parse_window(args.window)
    wh = window_hours(win)
    codes = resolve_stations(args.stations)
    print(f"計画 {args.plan}: 地点 {len(codes)} / 時別値ページ {n_h:,} + 平年値ページ {n_n} = {len(tasks):,}"
          f"（取得済み {cached:,}、残り {len(tasks) - cached:,}）")
    for iv in (1.0, 1.5, 2.0):
        h = (len(tasks) - cached) * iv / 3600
        s = f"  間隔 {iv} 秒: {h:.1f} 時間"
        if win:
            s += f"（窓 {wh:.0f} 時間/晩 → {math.ceil(h / wh)} 晩）"
        print(s)
    print(f"  cache: {cache}  ledger: {Ledger(cache / '_requests.log').n} 件")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="時別の平年値（気温）を作る")
    ap.add_argument("cmd", choices=["plan", "fetch", "build", "show"])
    ap.add_argument("--stations", default="home",
                    help="国際地点番号のカンマ区切り / home=HOME_CITIES 10 都市（既定）/ main=官署 57")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--plan", choices=["sample", "full"], default="sample")
    ap.add_argument("--years", default="", help="sample の標本年（カンマ区切り）。既定 1992,1995,...,2019")
    ap.add_argument("--days-per-month", type=int, default=DEFAULT_DAYS_PER_MONTH,
                    help="sample: 1 地点 × 1 月あたりの標本日数")
    ap.add_argument("--months", default="", help="対象月（カンマ区切り。既定 全部）")
    ap.add_argument("--seed", default="hourly-normals-1")
    ap.add_argument("--normals", choices=["master", "etrn"], default="master")
    ap.add_argument("--window", default="", help="通信してよい時間帯 HH:MM-HH:MM（JST）例 23:00-06:00")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="リクエスト間隔（秒、1.0 以上）")
    ap.add_argument("--max-requests", type=int, default=0,
                    help="cache dir の累計リクエスト数の上限（0=無制限）")
    ap.add_argument("--limit", type=int, default=0, help="今回送る最大リクエスト数（0=無制限）")
    ap.add_argument("--via-worker", action="store_true",
                    help="fetch: 気象庁へは Cloudflare の Worker（POST /etrn）から取りに行く。R2 にも残る")
    ap.add_argument("--max-attempts", type=int, default=4, help="1 ページあたり、失敗を何回で断念するか")
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA, help="build: 日方向の平滑化 σ（日）")
    ap.add_argument("--min-samples", type=int, default=8,
                    help="build: ±15 日内の標本日がこれ未満の日は出力しない")
    ap.add_argument("--no-amp-scale", dest="amp_scale", action="store_false",
                    help="build: 日較差の平年による振幅補正をしない")
    ap.add_argument("--date", default="07-03", help="show: MM-DD")
    args = ap.parse_args()
    return {"plan": cmd_plan, "fetch": cmd_fetch, "build": cmd_build, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
