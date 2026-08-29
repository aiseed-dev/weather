#!/usr/bin/env python3
"""アメダス収集 Worker を Cloudflare へデプロイする。

どこから実行してもよい（パスは自分で解決する）:

    python WeatherStatic/deploy_worker.py            # 下見
    python WeatherStatic/deploy_worker.py --apply    # 実際に上げる

二種類の秘密を混ぜない
    Cloudflare API トークン … デプロイする権限。Cloudflare アカウントを操作できる
    Worker の TOKEN         … tgsvr だけが Worker を呼べるようにする合言葉

    後者に前者を使ってはいけない。合言葉は tgsvr の設定ファイルと Worker の
    環境の両方に置かれ、呼び出しのたびに HTTP ヘッダで飛ぶ。漏れたときに
    失うものを「その Worker を呼べる」だけに留める。

読む場所（先に見つかったものを使う。値は表示しない）
    Cloudflare 資格情報 … 環境変数 → ~/.config/cloudflare/worker.env
                          → cf-publish 既定（~/.config/cloudflare/pages.env）
    合言葉              … 環境変数 → ~/.config/cloudflare/worker.env
                          → ~/.config/weather/points.env

    Worker のデプロイには Pages 用と違う権限（Workers Scripts: Edit、
    バケット作成には Workers R2 Storage: Edit）が要るので、トークンを
    分ける場合は worker.env に置く。

    合言葉は Worker 側の secret と tgsvr が送る値が**同じでなければならない**。
    手で二度打つと食い違うので、ここで読んだものをそのまま secret にする。
    cf-publish へは同名の環境変数として渡し、引数には置かない（ps で見える）。
    まだ無ければ --init-token で作れる。生成した値も表示しない。
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

# Worker 用の資格情報。Pages 用とは権限が違うので分けて置ける
CF_ENV = Path.home() / ".config" / "cloudflare" / "worker.env"
# tgsvr に配る設定。合言葉と Worker の URL が入る
ENV_FILE = Path.home() / ".config" / "weather" / "points.env"

TOKEN_KEY = "WEATHER_WORKER_TOKEN"
URL_KEY = "WEATHER_WORKER_URL"
CF_KEYS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE を読む。値はここでも呼び出し側でも表示しない。"""
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def find_token() -> tuple[str | None, str]:
    """合言葉と、それをどこから読んだか。値は返すが表示はしない。"""
    if os.environ.get(TOKEN_KEY):
        return os.environ[TOKEN_KEY], "環境変数"
    for path in (CF_ENV, ENV_FILE):
        v = read_env_file(path).get(TOKEN_KEY)
        if v:
            return v, str(path)
    return None, ""


def write_env_file(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """KEY=VALUE を書き戻す。値は表示しない。パーミッションは 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)


def init_token() -> int:
    token, where = find_token()
    if token:
        log(f"{TOKEN_KEY} は既にあります（{where}／値は表示しません）。"
            "作り直すならそこから消してください")
        return 1
    values = read_env_file()
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

    token, where = find_token()
    if not token:
        log(f"{TOKEN_KEY} がありません（環境変数 / {CF_ENV} / {ENV_FILE}）")
        log("  作るには: python WeatherStatic/deploy_worker.py --init-token")
        return 1
    log(f"合言葉: {where} から読みました（値は表示しません）")

    cmd = [find_cf_publish(), "worker", "deploy", str(WORKER_DIR),
           "--secret", "TOKEN", "--workers-dev"]
    if not apply:
        cmd.append("--dry-run")

    # 合言葉は同名の環境変数として渡す。引数に置くと ps で見えてしまう
    env = dict(os.environ, TOKEN=token)

    # Cloudflare の資格情報。Worker のデプロイには Pages 用と違う権限が要るので、
    # worker.env があればそちらを優先する。無ければ cf-publish が既定の
    # pages.env を読む。
    cf = read_env_file(CF_ENV)
    supplied = [k for k in CF_KEYS if cf.get(k)]
    if supplied:
        env.update({k: cf[k] for k in supplied})
        log(f"Cloudflare 資格情報: {CF_ENV} から {'・'.join(supplied)}")
    elif any(os.environ.get(k) for k in CF_KEYS):
        log("Cloudflare 資格情報: 環境変数")
    else:
        log("Cloudflare 資格情報: cf-publish の既定（~/.config/cloudflare/pages.env）")
        log("  Worker のデプロイには Workers Scripts: Edit が要ります。"
            "Pages 用のトークンでは足りません")
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
