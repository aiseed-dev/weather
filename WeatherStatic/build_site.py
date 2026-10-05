#!/usr/bin/env python3
"""site を生成し、点検して、Cloudflare Pages へ上げる。

    dev    make_testdata.py   作り物のデータで見た目を確かめる
    dev    sync_to_server.py   ソースを送る
    deb2  build_site.py      実データで生成・点検・公開      ← これ
    dev    release.py         手元から公開する（臨時・確認用）

**定期公開はここが行う。** サイトは 10 分ごとに更新されるので、生成だけして
誰も上げない形は成り立たない。cron から --publish で呼ぶ。そのため deb2 の
トークンには Pages:Edit が要る。

release.py（手元から）は、deb2 の生成物を取り寄せて上げる別経路。定期運用は
こちらではなく build_site.py --publish。手元からの公開は、deb2 を経由せずに
確かめたいときや、cron が止まっているときの手当てに使う。

点検を通らなければ公開しない
    - index.html が無い / Cloudflare の上限（20,000 ファイル・25 MiB）超え
    - ページ数が目安（500）を下回る（生成が途中で失敗している）
    - master/stations.json の _meta.source が TESTDATA（作り物）
    generate_status.py が 10 分値を見つけられず区画を飛ばした場合も、
    向こうが終了コード 1 を返すのでここで止まる。古い実況は公開されない。

使い方
    python build_site.py               # 生成して点検（公開しない）
    python build_site.py --publish     # 生成・点検・公開（cron はこれ）
    python build_site.py --dry-run     # 上げる中身を見る
    python build_site.py --skip-build --publish   # 生成済みを公開
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "public"
MASTER = BASE / "master"

PROJECT = os.environ.get("WEATHER_CF_PROJECT", "weather")

# Cloudflare Pages（無料枠）の制限。超えると公開が途中で失敗する
MAX_FILES = 20_000
MAX_FILE_BYTES = 25 * 1024 * 1024

# 生成が壊れていないことの目安。実データなら 3,000 ページ規模になる
MIN_PAGES = 500


def find_cf_publish() -> str:
    """cf-publish の場所。**実行中のインタプリタと同じ bin を先に見る。**

    venv の場所を決め打ちすると、別の venv から呼んだときに古い cf-publish が
    使われる。実際 旧サーバー(tgsvr) には venv が 2 つあり、片方だけ更新されていた
    （2026-08-29: ~/dev/weather/.venv が 0.3.1、WeatherStatic/.venv が 0.3.0）。
    """
    here = Path(sys.executable).parent / "cf-publish"
    if here.is_file():
        return str(here)
    from shutil import which
    return which("cf-publish") or "cf-publish"


def fail(*lines: str) -> int:
    """断る理由を出す。標準出力を先に流さないと、ログに落としたとき
    点検結果と理由の順序が入れ替わって読めなくなる。"""
    sys.stdout.flush()
    for line in lines:
        print(line, file=sys.stderr, flush=True)
    return 1


def run(label: str, args: list[str]) -> None:
    """1 手ずつ動かす。失敗したらそこで止める（次に進むと状態が中途半端になる）。"""
    print(f"\n=== {label} ===", flush=True)
    r = subprocess.run(args, cwd=BASE)
    if r.returncode != 0:
        raise SystemExit(f"{label} が失敗しました（終了コード {r.returncode}）。ここで止めます。")


def data_source() -> str:
    """master/stations.json が実データ由来か作り物かを返す。"""
    p = MASTER / "stations.json"
    if not p.is_file():
        return "(master/stations.json がありません)"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["_meta"].get("source", "(不明)")
    except Exception as e:
        return f"(読めません: {e})"


def inspect(public: Path = PUBLIC) -> dict:
    """公開前に生成物を点検する。壊れたものを上げないための関門。
    release.py からも同じ物差しで呼ぶ。"""
    if not public.is_dir():
        raise SystemExit(f"{public} がありません。")

    files = [p for p in public.rglob("*") if p.is_file()]
    pages = [p for p in files if p.suffix == ".html"]
    total = sum(p.stat().st_size for p in files)
    biggest = max(files, key=lambda p: p.stat().st_size, default=None)

    print(f"\n=== {public.name}/ の点検 ===")
    print(f"  HTML          {len(pages):,} ページ")
    print(f"  ファイル総数  {len(files):,} / 上限 {MAX_FILES:,}")
    print(f"  総容量        {total / 1024 / 1024:.1f} MiB")
    if biggest:
        size = biggest.stat().st_size
        print(f"  最大ファイル  {biggest.relative_to(public)} "
              f"({size / 1024 / 1024:.2f} MiB / 上限 {MAX_FILE_BYTES // 1024 // 1024} MiB)")

    problems = []
    if not (public / "index.html").is_file():
        problems.append("index.html がありません")
    if len(files) > MAX_FILES:
        problems.append(f"ファイル数が上限 {MAX_FILES:,} を超えています")
    over = [p for p in files if p.stat().st_size > MAX_FILE_BYTES]
    if over:
        problems.append(f"25 MiB を超えるファイルが {len(over)} 件"
                        f"（例 {over[0].relative_to(public)}）")
    return {"pages": len(pages), "files": len(files), "problems": problems}


def publish(project: str, dry: bool) -> int:
    """Cloudflare Pages へ上げる。定期運用ではここまでが 1 続き。

    サイトは 10 分ごとに更新されるので、生成だけして誰も上げない形は
    成り立たない。cron から --publish で呼ぶ。
    """
    cmd = [find_cf_publish(), "public", "--project", project]
    if dry:
        cmd.append("--dry-run")
    run("cf-publish" + ("（下見）" if dry else "（公開）"), cmd)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="site を生成して点検し、必要なら公開する")
    ap.add_argument("--skip-build", action="store_true", help="生成を飛ばして点検だけ")
    ap.add_argument("--publish", action="store_true",
                    help="点検を通ったら Cloudflare Pages へ上げる（cron はこれ）")
    ap.add_argument("--dry-run", action="store_true", help="公開はせず、上げる中身を見る")
    ap.add_argument("--project", default=PROJECT,
                    help=f"Pages プロジェクト名（既定 {PROJECT}）")
    args = ap.parse_args()

    source = data_source()
    print(f"データ: {source}" + ("  ← 作り物です" if source == "TESTDATA" else ""))

    if not args.skip_build:
        run("generate.py", [sys.executable, "generate.py"])
        run("generate_status.py", [sys.executable, "generate_status.py"])

    info = inspect()

    if info["problems"]:
        print("\n点検で問題が見つかりました:")
        for p in info["problems"]:
            print(f"  - {p}")
        return 1

    if source != "TESTDATA" and info["pages"] < MIN_PAGES:
        return fail("", f"ページ数が {info['pages']} 件しかありません（目安 {MIN_PAGES} 件以上）。",
                    "  生成が途中で失敗している可能性があります。")

    if not (args.publish or args.dry_run):
        print("\n生成と点検が終わりました。公開はしていません。")
        if source == "TESTDATA":
            print("  作り物のデータなので、これは公開できません（見た目の確認用）。")
        else:
            print("  公開するには --publish を付けてください。")
        return 0

    # --- ここから先は外向きの操作 ---
    if source == "TESTDATA":
        return fail("", "作り物のデータで生成されています。公開しません。",
                    "  実データのある環境で生成してから公開してください。")
    return publish(args.project, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
