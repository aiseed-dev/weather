#!/usr/bin/env python3
"""アメダス地点別 10 分値を集める。tgsvr で操作する道具。

流れ
    1. 気象庁の latest_time.txt で最新スロットを知る（1 リクエスト）
    2. 親 Worker を起こす。子がエリアごとに取って R2 へ置く
    3. R2 から公開ツリー public_amedas/point/ へ取り寄せる
    4. 地点別に無い要素だけ map JSON から取る（1 リクエスト）

なぜ一部だけ map か
    地点別エンドポイントに積雪が無い（2026-08-29 実測）。map にしか無い
    要素だけをそこから取る。map は全地点で 1 ファイルなので 10 分ごとに
    取っても 1 リクエストで、地点別の 1,286 に比べれば無視できる。
    合流は weatherlib/pointstore.py が行う。

なぜ Worker に取らせるか
    1,286 地点を tgsvr から 1 秒間隔で取ると 21 分かかる。Worker なら並列に
    走る。tgsvr は「いつ・何を取るか」を決め、重い取得は手足に任せる。

なぜ cron を Worker に持たせないか
    日付の切り替わりや取りこぼしの追跡を 1 箇所（tgsvr）に集めるため。
    Worker は呼ばれたぶんだけ働く。

数の勘定（無料枠はサブリクエスト 50/実行。R2 の put も数に入る）
    子 … 担当エリアの地点数ぶんの fetch ＋ put 1。最大エリア 47 で 48
    親 … 子の呼び出し数。1 回 45 まで。64 エリアなので 2 回に分けて呼ぶ

資格情報と URL
    WEATHER_WORKER_URL   親 Worker。既定の *.workers.dev で足りる
    WEATHER_WORKER_TOKEN 呼び出しの合言葉
    AMEDAS_R2_BASE       R2 の公開ベース。**取り寄せる場合だけ**要る
    環境変数か ~/.config/weather/points.env から読む。値は表示しない。

R2 に独自ドメインは要らない
    Worker → R2 はバインディングで書くので、ドメインが無くても収集は動く。
    ドメインが要るのは、ブラウザや Flet が HTTP で読みに行くときだけ。
    tgsvr が引き戻す必要も本来は無い（R2 から端末が直接読む）。--pull-only を
    使わなければ取り寄せは飛ばせる。

使い方
    python fetch_points.py                 # 最新スロットを取りに行く
    python fetch_points.py --pull-only     # 起動はせず R2 から取り寄せるだけ
    python fetch_points.py --day 20260829 --hour 12   # ブロックを指定
    python fetch_points.py --dry-run       # 何をするかだけ見る
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
POINT = BASE / "public_amedas" / "point"
EXTRA = BASE / "public_amedas" / "extra"
ENV_FILE = Path.home() / ".config" / "weather" / "points.env"

LATEST_TIME = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
MAP = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"
UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)"

MAX_AREAS_PER_CALL = 45      # 親 1 実行のサブリクエスト上限に合わせる
MAX_STATIONS_PER_AREA = 49   # 子 1 実行（fetch N + put 1 ≤ 50）


def log(msg: str) -> None:
    print(f"[points] {msg}", flush=True)


def load_env() -> None:
    """~/.config/weather/points.env の KEY=VALUE を環境へ。値は表示しない。"""
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_slot() -> datetime:
    """気象庁の最新スロット。ここだけは気象庁に直接聞く（1 リクエスト）。"""
    txt = http(LATEST_TIME, timeout=20).decode("utf-8").strip()
    return datetime.fromisoformat(txt)


def area_groups() -> dict[str, list[str]]:
    """エリア → 地点番号。master/area_map.json が正。"""
    p = MASTER / "area_map.json"
    if not p.is_file():
        sys.exit(f"{p} がありません。build_area_map.py を先に流してください。")
    groups: dict[str, list[str]] = defaultdict(list)
    for amedas, rec in json.loads(p.read_text(encoding="utf-8"))["stations"].items():
        groups[rec["area"]].append(amedas)
    over = {a: len(v) for a, v in groups.items() if len(v) > MAX_STATIONS_PER_AREA}
    if over:
        sys.exit(f"1 エリアの地点が多すぎます（上限 {MAX_STATIONS_PER_AREA}）: {over}")
    return {a: sorted(v) for a, v in sorted(groups.items())}


def invoke(groups: dict[str, list[str]], day: str, hour: str,
           dry: bool) -> list[str]:
    """親 Worker を起こす。戻り値は置けなかったエリア。"""
    base = os.environ.get("WEATHER_WORKER_URL", "").rstrip("/")
    token = os.environ.get("WEATHER_WORKER_TOKEN", "")
    # 下見では資格情報を要求しない。何をするかは値が無くても示せる
    if not dry and (not base or not token):
        sys.exit("WEATHER_WORKER_URL と WEATHER_WORKER_TOKEN が要ります"
                 f"（環境変数か {ENV_FILE}）")

    items = [{"area": a, "stations": s} for a, s in groups.items()]
    batches = [items[i:i + MAX_AREAS_PER_CALL]
               for i in range(0, len(items), MAX_AREAS_PER_CALL)]
    log(f"親を {len(batches)} 回呼ぶ（{len(items)} エリア / "
        f"{sum(len(s) for s in groups.values())} 地点）")
    if dry:
        for i, b in enumerate(batches, 1):
            mx = max(len(a["stations"]) for a in b)
            log(f"  [下見] {i} 回目: {len(b)} エリア（親のサブリクエスト {len(b)}／上限 45）"
                f" 最大エリア {mx} 地点（子のサブリクエスト {mx + 1}／上限 50）")
        log(f"  [下見] 設定: WORKER_URL={'あり' if base else 'なし'}"
            f" TOKEN={'あり' if token else 'なし'}")
        return []

    failed: list[str] = []
    for i, b in enumerate(batches, 1):
        body = json.dumps({"day": day, "hour": hour, "areas": b}).encode()
        try:
            res = json.loads(http(f"{base}/fetch", data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"}))
        except urllib.error.HTTPError as e:
            log(f"  {i} 回目: 失敗 HTTP {e.code} {e.read()[:120].decode(errors='replace')}")
            failed += [a["area"] for a in b]
            continue
        except Exception as e:
            log(f"  {i} 回目: 失敗 {e}")
            failed += [a["area"] for a in b]
            continue
        log(f"  {i} 回目: 置いた {res.get('written', 0)} エリア / "
            f"取得 {res.get('stored', 0)} / 欠測 {res.get('missing', 0)} / "
            f"失敗 {res.get('error', 0)}")
        failed += res.get("failed") or []
    return failed


def pull(day: str, hour: str, areas: list[str], dry: bool) -> int:
    """R2 から公開ツリーへ取り寄せる。R2 は読み取り（Class B）なので余裕がある。"""
    base = os.environ.get("AMEDAS_R2_BASE", "").rstrip("/")
    if not dry and not base:
        sys.exit(f"AMEDAS_R2_BASE が要ります（環境変数か {ENV_FILE}）")
    out = POINT / day / hour
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
    n = 0
    for a in areas:
        url = f"{base}/point/{day}/{hour}/{a}.json"
        if dry:
            continue
        try:
            body = http(url, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"  警告: {a} の取り寄せに失敗 HTTP {e.code}")
            continue
        except Exception as e:
            log(f"  警告: {a} の取り寄せに失敗 {e}")
            continue
        (out / f"{a}.json").write_bytes(body)
        n += 1
    return n


def fetch_extra(ts: datetime, dry: bool) -> int:
    """地点別に無い要素を map から補うために、map JSON を 1 本取る。

    **要素の一覧は持たない。** 欠測のときは要素そのものが来ないので、
    「いま無い」と「配信に無い」を静的な一覧で区別できない。値のあるものを
    そのまま残し、重ねる側（pointstore）で地点別を優先させる。

    weather は毎正時（分 00）のスロットにしか入らない。ここで特別扱いは
    しない——来た時刻には入り、来ない時刻には入らない、で正しい。

    map は全地点で 1 ファイルなので、10 分ごとに取っても 1 リクエスト。
    """
    key = ts.strftime("%Y%m%d%H%M")
    if dry:
        log(f"  [下見] map を 1 本取って補完分にする（{key}）")
        return 0
    try:
        payload = json.loads(http(MAP.format(ts=key + "00"), timeout=30))
    except urllib.error.HTTPError as e:
        log(f"  警告: map の取得に失敗 HTTP {e.code}")
        return 0
    except Exception as e:
        log(f"  警告: map の取得に失敗 {e}")
        return 0

    out, keys = {}, set()
    for amedas, entry in payload.items():
        vals = {k: v for k, v in entry.items()
                if isinstance(v, list) and v and v[0] is not None}
        if vals:
            out[amedas] = vals
            keys |= set(vals)
    d = EXTRA / key[:8]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps(out, ensure_ascii=False),
                                   encoding="utf-8")
    size = (d / f"{key}.json").stat().st_size
    log(f"補完分: {len(out)} 地点 / 要素 {len(keys)} 種 / {size:,} bytes"
        + ("（正時なので weather を含む）" if key[10:12] == "00" and "weather" in keys else ""))
    return len(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="アメダス地点別 10 分値を集める")
    ap.add_argument("--day", metavar="YYYYMMDD", help="対象日（既定は最新スロットの日）")
    ap.add_argument("--hour", metavar="HH", help="3 時間ブロック（00/03/…/21）")
    ap.add_argument("--pull", action="store_true",
                    help="R2 から tgsvr へ取り寄せる（既定はしない。"
                         "端末が R2 を直接読むなら不要）")
    ap.add_argument("--pull-only", action="store_true", help="Worker は起こさず取り寄せるだけ")
    ap.add_argument("--dry-run", action="store_true", help="何をするかだけ見る")
    args = ap.parse_args()

    load_env()
    groups = area_groups()

    slot: datetime | None = None
    if args.day and args.hour:
        day, hour = args.day, args.hour   # 過去分の取り直し。map は最新しか無い
    else:
        slot = latest_slot()
        day, hour = slot.strftime("%Y%m%d"), f"{slot.hour // 3 * 3:02d}"
        log(f"気象庁の最新スロット: {slot:%Y-%m-%d %H:%M}（ブロック {hour}）")

    failed: list[str] = []
    if not args.pull_only:
        failed = invoke(groups, day, hour, args.dry_run)
        if failed:
            log(f"置けなかったエリア {len(failed)} 件: {'、'.join(failed[:8])}"
                + ("…" if len(failed) > 8 else ""))

    if not args.pull_only and slot is not None:
        fetch_extra(slot, args.dry_run)

    # 取り寄せは既定で行わない。R2 に置いた時点で端末（Flet・ブラウザ）は
    # そこから直接読める。同じデータを tgsvr へ引き戻すのは二度手間で、
    # R2 の公開ドメインも要求してしまう。
    got = pull(day, hour, list(groups), args.dry_run) if args.pull else 0
    if args.dry_run:
        if args.pull or args.pull_only:
            log(f"[下見] {len(groups)} エリアを {POINT / day / hour}/ へ取り寄せる")
        return 0

    if args.pull or args.pull_only:
        log(f"取り寄せ: {got} / {len(groups)} エリア → {POINT / day / hour}/")
        if got == 0:
            log("1 エリアも取れませんでした。R2 と Worker の状態を確認してください")
            return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
