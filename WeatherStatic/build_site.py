#!/usr/bin/env python3
"""site を生成して点検する。tgsvr で操作する道具（手元でも生成の確認に使える）。

公開手順は dev(テストデータ) → tgsvr(実データ) → Cloudflare の 3 段。
このスクリプトは 2 段目まで。Cloudflare へ上げるのは手元の release.py。

    dev    make_testdata.py   作り物のデータで見た目を確かめる
    dev    sync_to_tgsvr.py   ソースを送る
    tgsvr  build_site.py      実データで生成して点検する      ← これ
    dev    release.py         生成物を取り寄せて Cloudflare へ

なぜ公開まで持たないか
    Cloudflare への送信は単なるアップロードで、tgsvr である必要がない。
    tgsvr にも Cloudflare のトークンはあるが、あれはデータ用（観測値の R2 同期と
    Worker 起動）。ここで Pages へ公開すると、そのトークンに Pages:Edit まで
    持たせることになる。公開を手元に置けば、tgsvr のトークンはデータ用の権限に
    絞ったままにできる。

使い方
    python build_site.py               # 生成して点検
    python build_site.py --skip-build  # 生成済みを点検するだけ
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "public"
MASTER = BASE / "master"

# Cloudflare Pages（無料枠）の制限。超えると公開が途中で失敗する
MAX_FILES = 20_000
MAX_FILE_BYTES = 25 * 1024 * 1024

# 生成が壊れていないことの目安。実データなら 3,000 ページ規模になる
MIN_PAGES = 500


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


def main() -> int:
    ap = argparse.ArgumentParser(description="site を生成して点検する")
    ap.add_argument("--skip-build", action="store_true", help="生成を飛ばして点検だけ")
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

    print("\n生成と点検が終わりました。")
    if source == "TESTDATA":
        print("  作り物のデータなので、これは公開できません（見た目の確認用）。")
    else:
        print("  公開は手元から: python release.py --publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
