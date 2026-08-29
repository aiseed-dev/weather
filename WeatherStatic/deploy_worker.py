#!/usr/bin/env python3
"""アメダス収集 Worker を Cloudflare へデプロイする。

どこから実行してもよい（パスは自分で解決する）:

    python WeatherStatic/deploy_worker.py            # 下見
    python WeatherStatic/deploy_worker.py --apply    # 実際に上げる

合言葉の扱い
    Worker 側の secret（TOKEN）と、tgsvr が送る WEATHER_WORKER_TOKEN は
    **同じ値でなければならない**。手で二度打つと食い違うので、
    ~/.config/weather/points.env の WEATHER_WORKER_TOKEN を読んで、それを
    そのまま Worker の secret にする。

    値は表示しない。cf-publish には同名の環境変数として渡すだけで、
    引数にも出さない（ps で見えないようにするため）。

    まだ無ければ --init-token で作れる。生成した値も表示しない。

必要な権限
    Workers Scripts: Edit      … Worker のデプロイ
    Workers R2 Storage: Edit   … バケット weather-amedas の作成
    Pages 用のトークンにはこれらが無い。足りなければ cf-publish が言う。
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORKER_DIR = BASE / "workers" / "amedas-point"
VENV = BASE / ".venv" / "bin" / "cf-publish"
ENV_FILE = Path.home() / ".config" / "weather" / "points.env"

TOKEN_KEY = "WEATHER_WORKER_TOKEN"
URL_KEY = "WEATHER_WORKER_URL"


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def read_env_file() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def write_env_file(values: dict[str, str]) -> None:
    """points.env を書き戻す。値は表示しない。パーミッションは 0600。"""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    ENV_FILE.write_text(body, encoding="utf-8")
    ENV_FILE.chmod(0o600)


def init_token() -> int:
    values = read_env_file()
    if values.get(TOKEN_KEY):
        log(f"{TOKEN_KEY} は既にあります（値は表示しません）。作り直すなら "
            f"{ENV_FILE} から消してください")
        return 1
    values[TOKEN_KEY] = secrets.token_urlsafe(32)
    write_env_file(values)
    log(f"{TOKEN_KEY} を作って {ENV_FILE} に書きました（値は表示しません）")
    log("  同じファイルを tgsvr にも置いてください。Worker と tgsvr で"
        "同じ値である必要があります")
    return 0


def find_cf_publish() -> str:
    if VENV.is_file():
        return str(VENV)
    from shutil import which
    found = which("cf-publish")
    if not found:
        sys.exit("cf-publish が見つかりません。"
                 f"{VENV} を作るか、PATH に入れてください")
    return found


def deploy(apply: bool) -> int:
    if not WORKER_DIR.is_dir():
        sys.exit(f"{WORKER_DIR} がありません")

    token = os.environ.get(TOKEN_KEY) or read_env_file().get(TOKEN_KEY)
    if not token:
        log(f"{TOKEN_KEY} がありません（環境変数か {ENV_FILE}）")
        log("  作るには: python WeatherStatic/deploy_worker.py --init-token")
        return 1

    cmd = [find_cf_publish(), "worker", "deploy", str(WORKER_DIR),
           "--secret", "TOKEN", "--workers-dev"]
    if not apply:
        cmd.append("--dry-run")

    # 合言葉は同名の環境変数として渡す。引数に置くと ps で見えてしまう
    env = dict(os.environ, TOKEN=token)
    log(("上げます" if apply else "下見します") + f": {WORKER_DIR.name}")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        print(f"  {line}")
    if r.returncode != 0:
        log(f"失敗しました（終了コード {r.returncode}）")
        return r.returncode

    if not apply:
        log("下見だけです。上げるには --apply を付けてください。")
        return 0

    url = next((m.group(0) for m in
                (re.search(r"https://\S+\.workers\.dev", line) for line in out.splitlines())
                if m), None)
    if url:
        log(f"公開 URL: {url}")
        values = read_env_file()
        if values.get(URL_KEY) != url:
            values[URL_KEY] = url
            write_env_file(values)
            log(f"{URL_KEY} を {ENV_FILE} に書きました")
        log("  同じファイルを tgsvr にも置いてください:")
        log(f"    scp {ENV_FILE} tgsvr:.config/weather/points.env")
    else:
        log("公開 URL を読み取れませんでした。上の出力を確認してください")

    log("次: ssh tgsvr 'cd dev/weather/WeatherStatic && "
        "./.venv/bin/python fetch_points.py --dry-run'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="アメダス収集 Worker をデプロイする")
    ap.add_argument("--apply", action="store_true", help="実際に上げる（既定は下見）")
    ap.add_argument("--init-token", action="store_true",
                    help=f"{TOKEN_KEY} を作って {ENV_FILE} に書く（値は表示しない）")
    args = ap.parse_args()
    return init_token() if args.init_token else deploy(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
