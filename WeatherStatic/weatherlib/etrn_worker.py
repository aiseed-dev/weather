"""過去の気象データ（etrn）のページを、Cloudflare の Worker（amedas-point の POST /etrn）経由で取る。

気象庁へは Cloudflare から行くので、まとまった数のページを取っても、deb2 の定常の取得
（実況・予報）が気象庁に止められる心配がない。Worker は取ったページを R2 の etrn/{key} にも置く。
Worker が取れるのは workers/amedas-point/worker.js の ETRN_PAGE に合う表示ページだけ。

Worker の URL と合言葉は環境変数（WEATHER_WORKER_URL・WEATHER_WORKER_TOKEN）か
~/.config/cloudflare/pages.env から読む。値は表示しない。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from weatherlib import jma

ENV_FILE = Path.home() / ".config" / "cloudflare" / "pages.env"
ETRN_PREFIX = "https://www.data.jma.go.jp/stats/etrn/view/"


def load_env() -> tuple[str, str]:
    """(Worker の URL, 合言葉)。無ければ SystemExit。"""
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    base = os.environ.get("WEATHER_WORKER_URL", "").rstrip("/")
    token = os.environ.get("WEATHER_WORKER_TOKEN", "")
    if not base or not token:
        raise SystemExit(f"--via-worker には WEATHER_WORKER_URL と WEATHER_WORKER_TOKEN が要ります（環境変数か {ENV_FILE}）")
    if not base.startswith("http"):
        base = "https://" + base
    return base, token


def fetch(url: str, key: str, base: str, token: str, *,
          user_agent: str = jma.USER_AGENT, timeout: float = jma.TIMEOUT + 15) -> tuple[int, bytes | None]:
    """etrn の URL（https://www.data.jma.go.jp/stats/etrn/view/…）を Worker 経由で取る。

    key は R2 に置くときの名前（etrn/ の下）。戻り値は (気象庁の HTTP ステータス, 本文)。
    通信できなければ (0, None)。"""
    if not url.startswith(ETRN_PREFIX):
        raise ValueError(f"etrn の表示ページではありません: {url}")
    data = json.dumps({"page": url.removeprefix(ETRN_PREFIX), "key": key}).encode()
    req = urllib.request.Request(base + "/etrn", data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return int(res.headers.get("X-Status", res.status)), res.read()
    except urllib.error.HTTPError as e:
        st = e.headers.get("X-Status") if e.headers else None
        return (int(st) if st and st.isdigit() else e.code), None
    except Exception:
        return 0, None
