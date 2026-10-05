#!/usr/bin/env python3
"""アメダス地点別 10 分値を集める。deb2 で操作する道具。

流れ
    1. 気象庁の latest_time.txt で最新スロットを知る（1 リクエスト）
    2. エリアごとに Worker を呼ぶ。Worker が R2 へ 1 本置き、同じバイト列を
       応答で返すので、deb2 の手元（public_amedas/point/）にも書く
    3. 地点別に無い要素だけ map JSON から取る（1 リクエスト）
    4. 地点別にしか無い要素（最大瞬間風速・日最高最低の起時など）の日別の記録を、
       まだ無い日の分だけ作る（weatherlib/pointdaily.py。手元のデータを消す前に）
    5. 手元の地点別・補完分は直近 KEEP_DAYS 日だけ残す（R2 は 30 日で自動削除）

なぜ Worker に取らせるか
    1,286 地点を deb2 から 1 秒間隔で取ると 21 分かかる。Worker なら並列に
    走る。deb2 は「いつ・何を取るか」を決め、重い取得は手足に任せる。
    Worker に cron は持たせない。日付の切り替わりや取りこぼしの追跡を
    1 箇所（deb2）に集めるため。

なぜ一部だけ map か
    地点別エンドポイントに積雪と天気が無い（2026-08-29 実測。A〜G の 12 地点で
    確認）。map にしか無い要素をそこから補う。map は全地点で 1 ファイルなので
    10 分ごとに取っても 1 リクエストで、地点別の 1,286 に比べれば無視できる。
    合流は weatherlib/pointstore.py が行う。

    要素の一覧は持たない。欠測のとき要素はキーごと来ないので、ある時刻に
    無いことは「配信に無い」とも「いま欠測」とも取れる。値のあるものを
    そのまま残す。

数の勘定（無料枠はサブリクエスト 50/実行。R2 の put も数に入る）
    Worker 1 実行 … 頼んだ地点数ぶんの fetch ＋ put 1。30 地点で 31
    呼ぶ数 … 72 回（64 エリア。多い県は 2 つに割れる）。同時 6 本
    実行 … 72 × 144 ＝ 10,368/日（上限 10 万）

    地点の多い県（岩手 47・長野 45・新潟 44 など 8 エリア）は 1 回 30 地点で
    切り、**別ファイル**（{area}-1.json / -2.json）に置く。ファイルを分ける
    ので既存を読んで併合する必要が無く、同一エリアでも並行に呼べる。
    読む側（pointstore）はブロック配下の *.json をすべて見る。

資格情報と URL
    WEATHER_WORKER_URL   Worker の URL。既定の *.workers.dev で足りる
    WEATHER_WORKER_TOKEN 呼び出しの合言葉。Worker 側の secret と同じ値
    AMEDAS_R2_BASE       R2 の公開ベース。**取り寄せる場合だけ**要る

    環境変数か ~/.config/cloudflare/pages.env から読む。値は表示しない。
    Cloudflare まわりは 1 マシン 1 ファイルなので、既にある pages.env に
    2 行足せばよい。合言葉は dev 側と同じ値でなければならない
    （deploy_worker.py が dev の値をそのまま Worker の secret にする）。

R2 に独自ドメインは要らない
    Worker → R2 はバインディングで書くので、ドメインが無くても収集は動く。
    ドメインが要るのは、ブラウザや Flet が HTTP で読みに行くときだけ。
    deb2 の手元へは Worker の応答から書く（"echo": true）ので、R2 からの
    取り寄せ（--pull）は、応答を取りこぼした分を後から埋めるときだけ使う。

使い方
    python fetch_points.py                 # 最新スロットを取りに行く
    python fetch_points.py --dry-run       # 何をするかだけ見る
    python fetch_points.py --day 20260829 --hour 12   # ブロックを指定
    python fetch_points.py --pull          # R2 から deb2 へも取り寄せる
    python fetch_points.py --pull-only     # 呼ばずに取り寄せるだけ
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
MASTER = BASE / "master"
POINT = BASE / "public_amedas" / "point"
EXTRA = BASE / "public_amedas" / "extra"
ENV_FILE = Path.home() / ".config" / "cloudflare" / "pages.env"

LATEST_TIME = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
MAP = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"
UA = "WeatherStaticFetcher/0.1 (site migration; contact: saki@yniji.net)"

# 1 回の呼び出しで頼む地点数の上限。Worker のサブリクエストは fetch N + put 1
# なので 49 まで可能だが、余裕を持たせて 30 で切る。実データ（64 エリア /
# 1,286 地点）では 8 エリアだけが 2 回に分かれ、呼び出しは 64 → 72 回になる。
CHUNK = 30
PARALLEL = 6                 # 同時に呼ぶ数。deb2 側の礼儀として控えめに
# 手元に残す日数。ページの生成は今日（0 時台は昨日）しか見ないが、障害の
# 調べ物用に少し残す。1 日 約 110MB（72 ファイル × 8 ブロック）
KEEP_DAYS = 3


def log(msg: str) -> None:
    print(f"[points] {msg}", flush=True)


def load_env() -> None:
    """~/.config/cloudflare/pages.env の KEY=VALUE を環境へ。値は表示しない。"""
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
    return {a: sorted(v) for a, v in sorted(groups.items())}


def chunks_of(stations: list[str]) -> list[list[str]]:
    """1 エリアを CHUNK 地点ずつに割る。端数が 1 件だけにならないよう均す。"""
    n = -(-len(stations) // CHUNK)          # 切り上げ
    if n <= 1:
        return [stations]
    size = -(-len(stations) // n)
    return [stations[i:i + size] for i in range(0, len(stations), size)]


def job_names(stations: list[str], area: str) -> list[tuple[str, list[str]]]:
    """1 エリアの (置き先の名前, 地点) の並び。分けたときは {area}-1, -2 …"""
    cs = chunks_of(stations)
    return [(area if len(cs) == 1 else f"{area}-{i}", part) for i, part in enumerate(cs, 1)]


def write_local(day: str, hour: str, name: str, body: bytes) -> None:
    """Worker が R2 に置いたのと同じバイト列を手元に書く。途中で読まれても
    壊れた JSON を見せないよう、一時ファイルに書いてから置き換える。"""
    out = POINT / day / hour
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f".{name}.json.tmp"
    tmp.write_bytes(body)
    tmp.replace(out / f"{name}.json")


def call_part(base: str, token: str, name: str, stations: list[str],
              day: str, hour: str) -> dict:
    """1 まとまりを Worker に取らせ、置いたものを手元にも書く。
    name がそのまま置き先のファイル名になる。"""
    body = json.dumps({"day": day, "hour": hour, "name": name,
                       "stations": stations, "echo": True}).encode()
    req = urllib.request.Request(f"{base}/fetch", data=body, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = r.read()
            h = r.headers
    except urllib.error.HTTPError as e:
        detail = e.read()[:120].decode(errors="replace")
        return {"name": name, "written": 0, "error": 1, "why": f"HTTP {e.code} {detail}"}
    except Exception as e:
        return {"name": name, "written": 0, "error": 1, "why": str(e)[:80]}

    if h.get("X-Written") is None:
        # echo を知らない古い Worker（本文は集計の JSON）。手元には書けない
        res = json.loads(payload)
        res["why"] = "Worker が古い（echo 未対応）。deploy_worker.py で入れ直す"
        return {**res, "written": 0, "error": res.get("error", 0) or 1}
    res = {"name": name, "stored": int(h["X-Stored"]), "missing": int(h["X-Missing"]),
           "error": int(h["X-Error"]), "written": int(h["X-Written"])}
    if res["written"]:
        try:
            json.loads(payload)            # 壊れたものを手元に置かない
        except ValueError:
            return {**res, "written": 0, "error": 1, "why": "応答の JSON が壊れている"}
        write_local(day, hour, name, payload)
    return res


def invoke(groups: dict[str, list[str]], day: str, hour: str,
           dry: bool, retries: int = 1) -> list[str]:
    """エリアごとに Worker を呼ぶ。戻り値は最後まで置けなかったエリア。

    結果がまとまり単位で返るので、失敗したものだけ呼び直せる。
    """
    base = os.environ.get("WEATHER_WORKER_URL", "").rstrip("/")
    token = os.environ.get("WEATHER_WORKER_TOKEN", "")
    if not dry and (not base or not token):
        sys.exit("WEATHER_WORKER_URL と WEATHER_WORKER_TOKEN が要ります"
                 f"（環境変数か {ENV_FILE}）")

    if dry:
        split = {a: len(chunks_of(v)) for a, v in groups.items() if len(chunks_of(v)) > 1}
        calls = sum(len(chunks_of(v)) for v in groups.values())
        big = max(groups, key=lambda a: len(groups[a]))
        mx = max(len(c) for v in groups.values() for c in chunks_of(v))
        log(f"  [下見] {len(groups)} エリア → 呼び出し {calls} 回（同時 {PARALLEL}）")
        if split:
            log(f"  [下見] 分ける {len(split)} エリア: "
                + "、".join(f"{a}×{n}" for a, n in list(split.items())[:6])
                + ("…" if len(split) > 6 else ""))
        # Worker のサブリクエストは fetch（地点数）＋ put 1
        log(f"  [下見] 最大 {mx} 地点（{big} は {len(groups[big])} 地点）"
            f" → サブリクエスト {mx + 1}／上限 50")
        log(f"  [下見] 設定: WORKER_URL={'あり' if base else 'なし'}"
            f" TOKEN={'あり' if token else 'なし'}")
        return []

    pending = dict(groups)
    failed: list[str] = []
    for attempt in range(retries + 1):
        if not pending:
            break
        if attempt:
            log(f"置けなかった {len(pending)} エリアを呼び直す（{attempt}/{retries}）")

        # 分けたぶんは別ファイルになるので、同一エリアでも並行に呼べる
        jobs = [(area, name, part) for area, stations in pending.items()
                for name, part in job_names(stations, area)]

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = {pool.submit(call_part, base, token, n, st, day, hour): a
                       for a, n, st in jobs}
            for f in as_completed(futures):
                r = f.result()
                r["area"] = futures[f]
                results.append(r)

        ok = [r for r in results if r.get("written")]
        ng = [r for r in results if not r.get("written")]
        log(f"  置いた {len(ok)} ファイル / 取得 {sum(r.get('stored', 0) for r in results)} 地点"
            f" / 欠測 {sum(r.get('missing', 0) for r in results)}"
            f" / 失敗 {sum(r.get('error', 0) for r in results)}")
        for r in ng[:5]:
            log(f"    × {r.get('name', r['area'])}: {r.get('why') or '1 地点も取れず'}")
        pending = {r["area"]: groups[r["area"]] for r in ng if r["area"] in groups}
        failed = list(pending)
    return failed


def pull(day: str, hour: str, names: list[str], dry: bool) -> int:
    """R2 から公開ツリーへ取り寄せる。R2 は読み取り（Class B）なので余裕がある。

    names は置き先のファイル名（分けたエリアは {area}-1, -2）。エリア名で
    探すと、分けて置いた 8 エリアが丸ごと抜ける。"""
    base = os.environ.get("AMEDAS_R2_BASE", "").rstrip("/")
    if not dry and not base:
        sys.exit(f"AMEDAS_R2_BASE が要ります（環境変数か {ENV_FILE}）")
    out = POINT / day / hour
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
    n = 0
    for a in names:
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


def prune_local(today: datetime, dry: bool) -> int:
    """手元の point/ と extra/ から、KEEP_DAYS 日より古い日付ディレクトリを消す。
    名前が YYYYMMDD のディレクトリだけを対象にする（R2 には全部残っている）。"""
    cutoff = (today - timedelta(days=KEEP_DAYS - 1)).strftime("%Y%m%d")
    n = 0
    for root in (POINT, EXTRA):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and len(d.name) == 8 and d.name.isdigit() and d.name < cutoff:
                if dry:
                    log(f"  [下見] 消す: {d}")
                else:
                    shutil.rmtree(d)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="アメダス地点別 10 分値を集める")
    ap.add_argument("--day", metavar="YYYYMMDD", help="対象日（既定は最新スロットの日）")
    ap.add_argument("--hour", metavar="HH", help="3 時間ブロック（00/03/…/21）")
    ap.add_argument("--pull", action="store_true",
                    help="R2 から deb2 へ取り寄せる（既定はしない。"
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
    # そこから直接読める。同じデータを deb2 へ引き戻すのは二度手間で、
    # R2 の公開ドメインも要求してしまう。
    names = [n for area, st in groups.items() for n, _ in job_names(st, area)]
    got = pull(day, hour, names, args.dry_run) if args.pull or args.pull_only else 0
    if args.dry_run:
        if args.pull or args.pull_only:
            log(f"[下見] {len(names)} ファイルを {POINT / day / hour}/ へ取り寄せる")
        if slot is not None:
            prune_local(slot, dry=True)
        return 0

    if args.pull or args.pull_only:
        log(f"取り寄せ: {got} / {len(names)} ファイル → {POINT / day / hour}/")
        if got == 0:
            log("1 エリアも取れませんでした。R2 と Worker の状態を確認してください")
            return 1
    # 日別の記録は、材料（手元の地点別データ）を消す前に作る
    from weatherlib import pointdaily
    try:
        pointdaily.catch_up(datetime.now())
    except Exception as e:                  # 記録の失敗で収集を止めない
        log(f"警告: 日別の記録を作れませんでした: {e}")
    if slot is not None:
        pruned = prune_local(slot, dry=False)
        if pruned:
            log(f"手元の古い日付 {pruned} 件を消しました（{KEEP_DAYS} 日分を残す）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
