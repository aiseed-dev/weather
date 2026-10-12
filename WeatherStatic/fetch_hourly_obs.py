#!/usr/bin/env python3
"""全地点の気温の時別値（1991〜2020 年）を取る。時刻別の平年値の材料（build_hourly_normals.py --source obs）。

なぜ要るか
    気象庁は時別の平年値を出していない。トップの地図と「気温の状況」の平年差を「その時刻の平年」との
    差にするため、地点ごとに 30 年分の時別値から作る（docs/data-acquisition.md §7）。
    日ごとのページ（etrn、1 ページ＝1 地点 1 日）で取ると約 1,000 万ページになるので、
    「過去の気象データ・ダウンロード」でまとめて取る。1 回の上限はおよそ 20 万時間なので、
    1 地点を 15 年ずつ 2 回（1991〜2005 年・2006〜2020 年）に分ける。

気象庁への配慮
    気象庁へは Cloudflare の Worker（amedas-point の POST /obsdl）から行く。Worker が取れるのは
    気温の時別値と地点の一覧だけ。一度に 1 回だけ取り、取るのにかかった時間以上（最低 --interval 秒）
    待ってから次を取る。--window で時間帯（JST）を限る。失敗は数分〜1 時間のバックオフ。

出力（data/ は .gitignore 済み）
    data/hourly_obs/{地点}/{開始年}-{終了年}.npz
        temp    int16  気温 ×10（欠測は -32768）。並びは 開始年 1 月 1 日 1 時 〜 終了年+1 年 1 月 1 日 0 時
                       （気象庁の「時」は 1〜24。24 時は翌日 0 時。build はこの 24 個を 1 日とする）
        quality int8   品質（8 正常・5 準正常・4 以下は資料不足や欠測、-1 は無い）
        homog   int8   均質番号（移転・測器の変更で増える。-1 は無い）
    data/hourly_obs/_ledger.jsonl  依頼の記録（成功・失敗・秒数）

使い方
    python fetch_hourly_obs.py plan   [--stations ...]              # 件数（通信なし）
    python fetch_hourly_obs.py fetch  --window 23:00-06:00 [--stations main10|kansho|amedas|all|番号,...]
    python fetch_hourly_obs.py show   --stations 47412               # 取れた範囲と品質
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from build_hourly_normals import (HOME_CITIES, Stop, _on_signal, in_window, log, parse_window,
                                  seconds_until_open, sleep_interruptible)
from weatherlib import etrn_worker, jma

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "hourly_obs"
MASTER = BASE / "master"
CHUNKS = [(1991, 2005), (2006, 2020)]
MIN_INTERVAL = 10.0
USER_AGENT = jma.USER_AGENT + " fetch_hourly_obs"
JST = timezone(timedelta(hours=9))
FILL = -32768


def targets(spec: str) -> list[tuple[int, dict]]:
    """取る地点（気温を測り、日別の平年値がある地点）。官署を先に、番号順。"""
    st = json.loads((MASTER / "stations.json").read_text(encoding="utf-8"))["stations"]
    all_ = [(int(c), r) for c, r in st.items()
            if r["elements"].get("temp") and r.get("etrn") and (MASTER / "normals" / f"{c}.json").is_file()]
    all_.sort(key=lambda x: (not x[1].get("intl"), x[0]))
    if spec == "all":
        return all_
    if spec == "main10":
        return [x for x in all_ if x[0] in HOME_CITIES]
    if spec == "kansho":
        return [x for x in all_ if x[1].get("intl")]
    if spec == "amedas":
        return [x for x in all_ if not x[1].get("intl")]
    want = {int(s) for s in spec.split(",") if s.strip()}
    return [x for x in all_ if x[0] in want]


def stid(rec: dict) -> str:
    e = rec["etrn"]
    return f"{e['type']}{e['block_no']}"


def out_path(code: int, chunk: tuple[int, int]) -> Path:
    return OUT / str(code) / f"{chunk[0]}-{chunk[1]}.npz"


def hours_in(chunk: tuple[int, int]) -> int:
    return int((datetime(chunk[1] + 1, 1, 1) - datetime(chunk[0], 1, 1)).total_seconds() // 3600)


def parse(body: bytes, chunk: tuple[int, int]) -> dict | None:
    """ダウンロードした CSV（cp932）→ 配列。形が違えば None。"""
    try:
        rows = list(csv.reader(io.StringIO(body.decode("cp932"))))
    except UnicodeDecodeError:
        return None
    if len(rows) < 6 or "気温" not in "".join(rows[3]):
        return None
    n = hours_in(chunk)
    t0 = datetime(chunk[0], 1, 1, 1)
    temp = np.full(n, FILL, dtype=np.int16)
    qual = np.full(n, -1, dtype=np.int8)
    homog = np.full(n, -1, dtype=np.int8)
    for r in rows[5:]:
        if len(r) < 2 or "/" not in r[0]:
            continue
        d, _, hms = r[0].partition(" ")
        y, m, dd = (int(x) for x in d.split("/"))
        h = int(hms.split(":")[0])
        when = datetime(y, m, dd) + timedelta(hours=h)
        i = int((when - t0).total_seconds() // 3600)
        if not 0 <= i < n:
            continue
        if r[1].strip():
            try:
                temp[i] = int(round(float(r[1]) * 10))
            except ValueError:
                pass
        if len(r) > 2 and r[2].strip().lstrip("-").isdigit():
            qual[i] = int(r[2])
        if len(r) > 3 and r[3].strip().lstrip("-").isdigit():
            homog[i] = int(r[3])
    return {"temp": temp, "quality": qual, "homog": homog}


def ledger(entry: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "_ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(JST).isoformat(timespec="seconds"), **entry},
                           ensure_ascii=False) + "\n")


def failures() -> dict[str, int]:
    p = OUT / "_ledger.jsonl"
    n: dict[str, int] = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if not e.get("ok"):
                n[e["key"]] = n.get(e["key"], 0) + 1
    return n


def cmd_plan(args) -> int:
    ts = targets(args.stations)
    todo = [(c, ch) for c, _ in ts for ch in CHUNKS if not out_path(c, ch).is_file()]
    log(f"地点 {len(ts)}（官署 {sum(1 for _, r in ts if r.get('intl'))}）× {len(CHUNKS)} 回 = "
        f"{len(ts) * len(CHUNKS)} 回 / 残り {len(todo)} 回")
    log(f"1 回 1〜2 分・待ちを同じだけとして、残りは約 {len(todo) * 3 / 60:.0f} 時間"
        f"（23:00〜06:00 の 7 時間/晩で約 {len(todo) * 3 / 60 / 7:.1f} 晩）")
    return 0


def cmd_fetch(args) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = open(OUT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("ほかの fetch が動いています（data/hourly_obs/.lock）。二重には動かしません")
        return 1
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    base, token = etrn_worker.load_env()
    win = parse_window(args.window)
    interval = max(args.interval, MIN_INTERVAL)
    ts = targets(args.stations)
    fails = failures()
    todo = [(c, r, ch) for c, r in ts for ch in CHUNKS
            if not out_path(c, ch).is_file() and fails.get(f"{c}-{ch[0]}-{ch[1]}", 0) < args.max_attempts]
    log(f"開始: 地点 {len(ts)} / 残り {len(todo)} 回（Cloudflare の Worker 経由、間隔 {interval:.0f} 秒以上、"
        f"窓 {args.window or 'なし'}）")
    done = n_ok = n_fail = 0
    backoff = 0.0
    for code, rec, ch in todo:
        if Stop.flag:
            break
        while win and not in_window(win, datetime.now(JST)) and not Stop.flag:
            wait = seconds_until_open(win, datetime.now(JST))
            log(f"窓の外です。{wait / 3600:.1f} 時間後に再開します")
            sleep_interruptible(wait + 5)
        if Stop.flag:
            break
        if args.limit and done >= args.limit:
            log(f"--limit {args.limit} に達しました")
            break
        key = f"{code}-{ch[0]}-{ch[1]}"
        t0 = time.monotonic()
        status, body = etrn_worker.obsdl({"kind": "hourly", "stid": stid(rec), "from": ch[0], "to": ch[1]},
                                         base, token, user_agent=USER_AGENT)
        took = time.monotonic() - t0
        data = parse(body, ch) if status == 200 and body else None
        done += 1
        if data is None:
            n_fail += 1
            ledger({"key": key, "ok": False, "status": status, "seconds": round(took, 1),
                    "bytes": len(body or b"")})
            backoff = min(max(backoff * 2, 300.0), 3600.0)
            log(f"  失敗 {rec['name']}（{key}）HTTP {status}。{backoff / 60:.0f} 分待ちます")
            sleep_interruptible(backoff)
            continue
        backoff = 0.0
        p = out_path(code, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, **data)
        tmp.replace(p)
        good = int(((data["quality"] >= 5) & (data["temp"] != FILL)).sum())
        n_ok += 1
        ledger({"key": key, "ok": True, "seconds": round(took, 1), "bytes": len(body), "good": good})
        if n_ok % 10 == 0 or n_ok <= 3:
            log(f"  {n_ok}/{len(todo)} {rec['name']}（{key}）{took:.0f} 秒・{len(body) // 1024}KB・"
                f"正常 {good / hours_in(ch):.0%}")
        sleep_interruptible(max(interval, took))
    log(f"終了: 成功 {n_ok}・失敗 {n_fail}（残りは同じコマンドで続きから）")
    return 0


def cmd_show(args) -> int:
    for code, rec in targets(args.stations):
        for ch in CHUNKS:
            p = out_path(code, ch)
            if not p.is_file():
                print(f"{code} {rec['name']} {ch[0]}-{ch[1]}: まだ")
                continue
            d = np.load(p)
            q, t = d["quality"], d["temp"]
            good = (q >= 5) & (t != FILL)
            homog = sorted(set(int(x) for x in d["homog"] if x >= 0))
            print(f"{code} {rec['name']} {ch[0]}-{ch[1]}: 正常 {good.mean():.1%}・"
                  f"均質番号 {homog}・{t[good].min() / 10:.1f}〜{t[good].max() / 10:.1f}℃")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["plan", "fetch", "show"])
    ap.add_argument("--stations", default="all", help="main10 | kansho | amedas | all | 番号,番号")
    ap.add_argument("--window", help="通信してよい時間帯（JST）HH:MM-HH:MM")
    ap.add_argument("--interval", type=float, default=30.0, help="次の依頼までの最低の秒数（10 以上）")
    ap.add_argument("--limit", type=int, default=0, help="今回の依頼数の上限")
    ap.add_argument("--max-attempts", type=int, default=3, help="同じ依頼を失敗してよい回数")
    args = ap.parse_args()
    return {"plan": cmd_plan, "fetch": cmd_fetch, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
