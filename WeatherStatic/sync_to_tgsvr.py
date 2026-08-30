#!/usr/bin/env python3
"""ソースを tgsvr へ送る。rsync を毎回手で打たないための道具。

送るもの
    git が知っているファイル。git ls-files を出処にしているので、.gitignore に
    ある store/ data/ public/ master/ logs/ は自動的に外れる。運用データは
    tgsvr のもの、ソースは手元のもの、という区切りが .gitignore と一致する。

    観測所情報 stations/ も送る。tgsvr の crontab は accumulate.py と
    backfill_etrn.py だけで watch_stations.py を回していないので、tgsvr が
    stations/ を書き換えることはない。検知用の控え .amedastable.prev.adoc は
    .gitignore にあるため、送っても tgsvr 側の基準は保たれる。

手元専用の道具（このファイルと release.py）は送らない。向こうに置かなければ
向こうでは動かせない。
2026-08-28 に dev 向けのコマンド列を tgsvr のシェルに貼って tgsvr→tgsvr の
rsync が走りかけたが、道具が向こうに無ければその事故は起こりようがない。

tgsvr でしかできないこと（実データでの生成と点検）は build_site.py。
あちらは tgsvr で動かす前提なので、送る対象に入れてある。

安全のための決まり
    - 既定は下見だけ。実際に送るのは --apply を付けたときだけ
    - 消す操作はしない。余分なファイルは報告するだけで、消すかは人が決める
    - 生成はしない。送るところで止める（続けて何が起きるか分からない状態にしない）

使い方
    python sync_to_tgsvr.py            # 何が変わるかを見るだけ
    python sync_to_tgsvr.py --apply    # 実際に送る
    python sync_to_tgsvr.py --stale    # 向こうに残っている余分なファイルを調べる
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent          # …/weather/WeatherStatic
REPO = BASE.parent                              # …/weather
# 手元でしか使わない道具は送らない。向こうに置かなければ向こうでは動かせない。
DEV_ONLY = {Path(__file__).name, "release.py"}

HOST = os.environ.get("WEATHER_SYNC_HOST", "tgsvr")
DEST = os.environ.get("WEATHER_SYNC_PATH", "dev/weather/WeatherStatic")
# 向こうの python。venv は repo 直下に 1 つ（WeatherStatic からは 1 つ上）
REMOTE_PY = os.environ.get("WEATHER_SYNC_PY", "../.venv/bin/python")

# 余分なファイルを探す範囲。運用データの置き場は見ない
STALE_SCAN = ("templates", "assets", "weatherlib", "workers", "wwwroot", "tests")


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"git {' '.join(args)} が失敗しました:\n{r.stderr.strip()}")
    return r.stdout


def source_files() -> tuple[list[str], list[str]]:
    """送るファイルの一覧（WeatherStatic からの相対パス）と、消えているものの一覧。"""
    out = git("ls-files", "-z", "--", "WeatherStatic")
    rels: list[str] = []
    missing: list[str] = []
    for entry in out.split("\0"):
        if not entry:
            continue
        rel = entry[len("WeatherStatic/"):]
        if not rel or rel in DEV_ONLY:
            continue
        if (BASE / rel).is_file():
            rels.append(rel)
        else:
            missing.append(rel)          # git にはあるが手元に無い（削除の途中など）
    return sorted(rels), sorted(missing)


def warn_uncommitted() -> None:
    """未コミットの変更があれば知らせる。送るのは作業ツリーの中身なので、
    「コミットした内容」と「送る内容」がずれていることを黙って通さない。"""
    dirty = [l for l in git("status", "--porcelain", "--", "WeatherStatic").splitlines()
             if l and not l.startswith("??")]
    if dirty:
        print(f"注意: 未コミットの変更が {len(dirty)} 件あります。作業ツリーの内容を送ります。")
        for l in dirty[:8]:
            print(f"    {l}")
        if len(dirty) > 8:
            print(f"    … 他 {len(dirty) - 8} 件")
        print()


def rsync(rels: list[str], apply: bool) -> int:
    cmd = ["rsync", "-a", "--itemize-changes", "--files-from=-",
           str(BASE) + "/", f"{HOST}:{DEST}/"]
    if not apply:
        cmd.insert(2, "--dry-run")
    r = subprocess.run(cmd, input="\n".join(rels), capture_output=True, text=True)
    if r.returncode != 0:
        die(f"rsync が失敗しました (終了コード {r.returncode}):\n{r.stderr.strip()}")

    # itemize-changes は変わったものだけ出す。書式は 11 桁の記号＋空白＋名前で、
    # 2 桁目が種別（f=ファイル d=ディレクトリ L=シンボリックリンク）。
    # ディレクトリは中身の更新に伴って必ず出るので、数えると実態とずれる。
    changed = []
    for line in r.stdout.splitlines():
        if len(line) < 12 or line[1] == "d":
            continue
        changed.append(line[11:].strip())
    if changed:
        head = "送りました" if apply else "送る予定"
        print(f"{head}: {len(changed)} ファイル")
        for name in changed:
            print(f"    {name}")
    else:
        print("差分はありません（向こうは既に同じ内容です）")
    return len(changed)


def report_stale(rels: list[str]) -> None:
    """向こうにあって、こちらの一覧に無いファイルを報告する。消しはしない。
    _layout_legacy.html のような「消したのに向こうに残っている」ものを見つける。"""
    scan = " ".join(f"'{d}'" for d in STALE_SCAN)
    r = subprocess.run(
        ["ssh", HOST, f"cd {DEST} && find {scan} -type f 2>/dev/null"],
        capture_output=True, text=True)
    # 向こうに無いディレクトリがあると find は非ゼロで終わるが、
    # 在るものは列挙できている。何も返らなかったときだけ異常とみなす。
    if not r.stdout.strip():
        print(f"\n向こうの一覧を取れませんでした（{DEST} を確認してください）")
        if r.stderr.strip():
            print(f"  {r.stderr.strip()}")
        return
    remote = {l.lstrip("./") for l in r.stdout.split() if l}
    here = set(rels)
    extra = sorted(f for f in remote - here if "__pycache__" not in f)
    if extra:
        print(f"\n向こうにだけあるファイル: {len(extra)} 件")
        for f in extra:
            print(f"    {f}")
        print("  こちらで削除したものが残っている可能性があります。")
        print(f"  消す場合は中身を確かめてから: ssh {HOST} 'cd {DEST} && rm …'")
    else:
        print("\n向こうにだけあるファイルはありません")


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{HOST} へソースを送る")
    ap.add_argument("--apply", action="store_true", help="実際に送る（既定は下見だけ）")
    ap.add_argument("--stale", action="store_true", help="向こうに残る余分なファイルも調べる")
    args = ap.parse_args()

    rels, missing = source_files()
    if missing:
        print(f"注意: git にはあるが手元に無いファイルが {len(missing)} 件（送りません）")
        for m in missing[:5]:
            print(f"    {m}")
        print()
    warn_uncommitted()

    print(f"対象 {len(rels)} ファイル → {HOST}:{DEST}/")
    n = rsync(rels, args.apply)

    if args.stale:
        report_stale(rels)

    if not args.apply:
        print("\n下見だけです。送るには --apply を付けてください。")
    elif n:
        print("\n送信は完了しました。次は向こうで生成して点検:")
        print(f"    ssh {HOST} 'cd {DEST} && {REMOTE_PY} build_site.py'")
        print("  問題なければ公開:")
        print(f"    ssh {HOST} 'cd {DEST} && {REMOTE_PY} build_site.py --publish'")
        print("  以後は cron が 10 分ごと・毎時に同じことを行う")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
