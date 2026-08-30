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

置き場所（値は表示しない）
    dev   ~/.config/cloudflare/worker.env  デプロイ用の資格情報と合言葉
    tgsvr ~/.config/cloudflare/pages.env   合言葉と Worker の URL
                                           （fetch_points.py が読む）

    Worker のデプロイには Pages 用と違う権限（Workers Scripts: Edit、
    バケット作成には Workers R2 Storage: Edit）が要るので、worker.env に
    分けて置く。無ければ cf-publish が既定の pages.env を読む。

    **worker.env を tgsvr へ丸ごと配らない。** 向こうの pages.env には
    向こうの資格情報が入っている。足すのは 2 行だけ。

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

# Worker 用。Cloudflare の資格情報（デプロイ権限）と合言葉をここに置く。
# Pages 用とは要る権限が違うので分けてある。
CF_ENV = Path.home() / ".config" / "cloudflare" / "worker.env"
# tgsvr 側で fetch_points.py が読むファイル。**丸ごと配らない** —
# 向こうの pages.env には向こうの資格情報が入っているので、
# WEATHER_WORKER_URL と WEATHER_WORKER_TOKEN の 2 行だけを足す。
REMOTE_ENV = "~/.config/cloudflare/pages.env"

TOKEN_KEY = "WEATHER_WORKER_TOKEN"
URL_KEY = "WEATHER_WORKER_URL"
CF_KEYS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def read_env_file(path: Path = CF_ENV) -> dict[str, str]:
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
    v = read_env_file(CF_ENV).get(TOKEN_KEY)
    return (v, str(CF_ENV)) if v else (None, "")


def write_env_file(values: dict[str, str], path: Path = CF_ENV) -> None:
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
    log(f"{TOKEN_KEY} を作って {CF_ENV} に書きました（値は表示しません）")
    log(f"  デプロイ後、tgsvr の {REMOTE_ENV} にも同じ値を足してください")
    return 0


def find_cf_publish() -> str:
    """cf-publish の場所。**実行中のインタプリタと同じ bin を先に見る。**

    venv の場所を決め打ちすると、別の venv から呼んだときに古い cf-publish が
    使われる。実際 tgsvr には venv が 2 つあり、片方だけ更新されていた。
    """
    here = Path(sys.executable).parent / "cf-publish"
    if here.is_file():
        return str(here)
    from shutil import which
    found = which("cf-publish")
    if not found:
        sys.exit("cf-publish が見つかりません。"
                 "この python と同じ venv に入れるか、PATH に入れてください")
    return found


def deploy(apply: bool) -> int:
    if not WORKER_DIR.is_dir():
        sys.exit(f"{WORKER_DIR} がありません")

    token, where = find_token()
    if not token:
        log(f"{TOKEN_KEY} がありません（環境変数 か {CF_ENV}）")
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
            log(f"{URL_KEY} を {CF_ENV} に書きました")
        # ファイルごと配ると向こうの資格情報を潰すので、2 行だけ足させる
        log(f"  tgsvr の {REMOTE_ENV} に次の 2 行を足してください（値は各自で）:")
        log(f"    {URL_KEY}={url}")
        log(f"    {TOKEN_KEY}=<{CF_ENV} と同じ値>")
    else:
        log("公開 URL を読み取れませんでした。上の出力を確認してください")

    log("次: ssh tgsvr 'cd dev/weather/WeatherStatic && "
        "../.venv/bin/python fetch_points.py --dry-run'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="アメダス収集 Worker をデプロイする")
    ap.add_argument("--apply", action="store_true", help="実際に上げる（既定は下見）")
    ap.add_argument("--init-token", action="store_true",
                    help=f"{TOKEN_KEY} を作って {CF_ENV} に書く（値は表示しない）")
    args = ap.parse_args()
    return init_token() if args.init_token else deploy(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
