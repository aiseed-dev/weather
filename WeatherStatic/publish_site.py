#!/usr/bin/env python3
"""site を生成し、必要なら Cloudflare Pages へ公開する。tgsvr で操作する道具。

公開手順は dev(テストデータ) → tgsvr(実データ) → Cloudflare の 3 段。
このスクリプトは 2 段目と 3 段目を受け持つ。ソースを送るのは手元の
sync_to_tgsvr.py（あちらは自分自身を送らないので、ここには無い）。

既定では公開しない
    引数なしなら生成して結果を見せるだけで止まる。公開は --publish を
    付けたときだけ。取り消しにくい操作を、確認の余地なく続けて走らせない。

作り物を公開しない
    master/stations.json の _meta.source が TESTDATA なら公開を拒む。
    手元のテストデータで生成したものが本番サイトに乗るのを防ぐ。
    生成だけは手元でもできるので、見た目の確認には使える。

資格情報は扱わない
    Cloudflare のトークンは cf-publish が ~/.config/cloudflare/pages.env
    から自分で読む。このスクリプトは値に触れないし、表示もしない。

使い方
    python publish_site.py                  # 生成して点検するだけ
    python publish_site.py --dry-run        # 生成し、上げる中身を下見
    python publish_site.py --publish        # 生成して公開
    python publish_site.py --skip-build --publish   # 生成済みを公開
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
MIN_PAGES_FOR_PUBLISH = 500


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


def inspect() -> dict:
    """公開前に public/ を点検する。壊れた生成物を上げないための関門。"""
    if not PUBLIC.is_dir():
        raise SystemExit(f"{PUBLIC} がありません。--skip-build を外して生成してください。")

    files = [p for p in PUBLIC.rglob("*") if p.is_file()]
    pages = [p for p in files if p.suffix == ".html"]
    total = sum(p.stat().st_size for p in files)
    biggest = max(files, key=lambda p: p.stat().st_size, default=None)

    print(f"\n=== public/ の点検 ===")
    print(f"  HTML          {len(pages):,} ページ")
    print(f"  ファイル総数  {len(files):,} / 上限 {MAX_FILES:,}")
    print(f"  総容量        {total / 1024 / 1024:.1f} MiB")
    if biggest:
        size = biggest.stat().st_size
        print(f"  最大ファイル  {biggest.relative_to(PUBLIC)} "
              f"({size / 1024 / 1024:.2f} MiB / 上限 {MAX_FILE_BYTES // 1024 // 1024} MiB)")

    problems = []
    if not (PUBLIC / "index.html").is_file():
        problems.append("index.html がありません")
    if len(files) > MAX_FILES:
        problems.append(f"ファイル数が上限 {MAX_FILES:,} を超えています")
    over = [p for p in files if p.stat().st_size > MAX_FILE_BYTES]
    if over:
        problems.append(f"25 MiB を超えるファイルが {len(over)} 件"
                        f"（例 {over[0].relative_to(PUBLIC)}）")
    return {"pages": len(pages), "files": len(files), "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser(description="site を生成し、必要なら公開する")
    ap.add_argument("--publish", action="store_true", help="Cloudflare Pages へ公開する")
    ap.add_argument("--dry-run", action="store_true",
                    help="公開はせず、上げる中身だけ見る")
    ap.add_argument("--skip-build", action="store_true", help="生成を飛ばす")
    ap.add_argument("--project", default=PROJECT, help=f"Pages プロジェクト名（既定 {PROJECT}）")
    args = ap.parse_args()

    py = sys.executable
    source = data_source()
    print(f"データ: {source}" + ("  ← 作り物です" if source == "TESTDATA" else ""))

    if not args.skip_build:
        run("generate.py", [py, "generate.py"])
        run("generate_status.py", [py, "generate_status.py"])

    info = inspect()

    if info["problems"]:
        print("\n公開を止めます。次の点を直してください:")
        for p in info["problems"]:
            print(f"  - {p}")
        return 1

    if not (args.publish or args.dry_run):
        print("\n生成と点検まで終わりました。公開はしていません。")
        print("  上げる中身を見る: python publish_site.py --skip-build --dry-run")
        print("  公開する        : python publish_site.py --skip-build --publish")
        return 0

    # --- ここから先は外向きの操作 ---
    if source == "TESTDATA":
        return fail("", "作り物のデータで生成されています。公開しません。",
                    "  実データのある環境（tgsvr）で生成してから公開してください。")

    if args.publish and info["pages"] < MIN_PAGES_FOR_PUBLISH:
        return fail("", f"ページ数が {info['pages']} 件しかありません"
                    f"（目安 {MIN_PAGES_FOR_PUBLISH} 件以上）。",
                    "  生成が途中で失敗している可能性があります。公開しません。")

    cf = BASE / ".venv" / "bin" / "cf-publish"
    cmd = [str(cf) if cf.is_file() else "cf-publish", "public", "--project", args.project]
    if args.dry_run:
        cmd.append("--dry-run")
    run("cf-publish" + ("（下見）" if args.dry_run else "（公開）"), cmd)

    if args.dry_run:
        print("\n下見だけです。公開するには --publish を付けてください。")
    else:
        print(f"\n公開しました: プロジェクト {args.project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
