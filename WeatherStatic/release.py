#!/usr/bin/env python3
"""dev2 から生成物を取り寄せて Cloudflare Pages へ公開する。手元で操作する道具。

公開手順 dev(テストデータ) → dev2(実データ) → Cloudflare の 3 段目。

    dev    make_testdata.py   作り物のデータで見た目を確かめる
    dev    sync_to_server.py   ソースを送る
    dev2  build_site.py      実データで生成して点検する
    dev    release.py         生成物を取り寄せて Cloudflare へ      ← これ

なぜ手元から上げるか
    Cloudflare への送信は単なるアップロードで、dev2 である必要がない。
    dev2 にも Cloudflare のトークンはあるが、あれはデータ用（観測値の R2 同期と
    Worker 起動）。site の公開までそこでやると、同じトークンに Pages:Edit まで
    持たせることになる。生成物を取り寄せる一手間と引き換えに、権限を
    データ用（dev2）と公開用（手元）に分けたままにできる。

置き場所
    取り寄せ先は release/（.gitignore 済み）。public/ とは分ける。
    public/ は手元ではテストデータの出力なので、混ざると何を公開したのか
    分からなくなる。

資格情報は扱わない
    Cloudflare のトークンは cf-publish が ~/.config/cloudflare/pages.env から
    自分で読む。このスクリプトは値に触れないし、表示もしない。
    手元に置いていなければ cf-publish が自分で知らせる。

このファイルは dev2 へ送らない（sync_to_server.py の DEV_ONLY を参照）。

使い方
    python release.py                  # 取り寄せて点検（公開しない）
    python release.py --dry-run        # 上げる中身を下見
    python release.py --publish        # 公開する
    python release.py --skip-pull --publish   # 取り寄せ済みを公開
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from build_site import MIN_PAGES, fail, find_cf_publish, inspect, run

BASE = Path(__file__).resolve().parent
RELEASE = BASE / "release"

HOST = os.environ.get("WEATHER_SYNC_HOST", "dev2")
REMOTE = os.environ.get("WEATHER_SYNC_PATH", "dev/weather/WeatherStatic") + "/public/"
PROJECT = os.environ.get("WEATHER_CF_PROJECT", "weather")


def pull(apply: bool = True) -> None:
    """dev2 の public/ を release/ に鏡写しする。

    --delete を付ける。消えたページが残ったまま公開されると、存在しないはずの
    地点ページが site に居座る。取り寄せ先は release/ 専用なので、
    ここでの削除は生成物の同期であって、手で作ったものを消す危険はない。
    """
    RELEASE.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete", "--info=stats2",
           f"{HOST}:{REMOTE}", str(RELEASE) + "/"]
    if not apply:
        cmd.insert(2, "--dry-run")
    print(f"\n=== {HOST} から取り寄せ ===", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"取り寄せに失敗しました:\n{r.stderr.strip()}")
    for line in r.stdout.splitlines():
        if any(k in line for k in ("Number of files", "Total file size",
                                   "Total transferred", "sent ", "deleted")):
            print(f"  {line.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser(description=f"生成物を取り寄せて Cloudflare へ公開する")
    ap.add_argument("--publish", action="store_true", help="実際に公開する")
    ap.add_argument("--dry-run", action="store_true", help="公開はせず、上げる中身を見る")
    ap.add_argument("--skip-pull", action="store_true", help="取り寄せを飛ばす")
    ap.add_argument("--project", default=PROJECT, help=f"Pages プロジェクト名（既定 {PROJECT}）")
    args = ap.parse_args()

    if not args.skip_pull:
        pull()
    elif not RELEASE.is_dir():
        return fail(f"{RELEASE} がありません。--skip-pull を外してください。")

    info = inspect(RELEASE)

    if info["problems"]:
        print("\n公開を止めます。次の点を直してください:")
        for p in info["problems"]:
            print(f"  - {p}")
        return 1

    if info["pages"] < MIN_PAGES:
        return fail("", f"ページ数が {info['pages']} 件しかありません（目安 {MIN_PAGES} 件以上）。",
                    "  dev2 の生成が途中で失敗しているか、作り物のデータで",
                    "  生成されている可能性があります。公開しません。")

    if not (args.publish or args.dry_run):
        print("\n取り寄せと点検が終わりました。公開はしていません。")
        print("  上げる中身を見る: python release.py --skip-pull --dry-run")
        print("  公開する        : python release.py --skip-pull --publish")
        return 0

    # --- ここから先は外向きの操作 ---
    cmd = [find_cf_publish(), str(RELEASE), "--project", args.project]
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
