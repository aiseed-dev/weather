"""自前のアクセス解析 kaiseki（aiai-tools の kaiseki/）をページに入れるかどうか。

受け口の URL（例: https://analytics.aiseed.dev）が環境変数 WEATHER_KAISEKI_TO か、設定ファイル
~/.config/weather-site/kaiseki.env（WEATHER_KAISEKI_TO=… の 1 行。cron を書き換えずに入れ切りできる）
にあるときだけ、
全ページの </head> の前にスクリプトを入れ、/kaiseki.js と知らせのページ /kaiseki/ を置く。
無ければ何も入れず、前に置いたものは消す。入れるか（公開するか）は運用者が決める。

kaiseki.js は aiai-tools の kaiseki/kaiseki.js の写し（assets/kaiseki.js、aiai-tools e3e2d58）。
直すときは aiai-tools 側で直して写し直す。受け口は deb2 の analytics.aiseed.dev。
ページが送る出来事（kaiseki.event）: mine_add・mine_remove（自分の地点、値はアメダス番号）、search（サイト内検索、
値は検索の言葉）、notfound（404、値は URL のパス）、stage_save（トップの設定の保存、値は流し方）、
map_tab（実況の地図の切り替え、値は地図の名前）。値の中身は /kaiseki/ に書く。ページを見た人の記録を受け口へ送るので、電気通信事業法の
外部送信規律の対象。送る情報・送り先・目的は /kaiseki/（templates/kaiseki.html）に書く。
"""
from __future__ import annotations

import os
from pathlib import Path

# ID を引き継ぐ自分のサイトのドメイン（kaiseki.js の data-own）
OWN = "time-j.net aiseed.dev"
CONFIG = Path.home() / ".config" / "weather-site" / "kaiseki.env"


def settings() -> dict | None:
    """{"to": 受け口, "own": ドメイン} か、入れないなら None。環境変数が設定ファイルより先。"""
    to = os.environ.get("WEATHER_KAISEKI_TO", "").strip()
    if not to and CONFIG.is_file():
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() == "WEATHER_KAISEKI_TO":
                to = v.strip()
    to = to.rstrip("/")
    if not to.startswith("https://") and not to.startswith("http://127.0.0.1"):
        return None                       # 書き間違いで変な所へ送らない（手元の試験だけ http を許す）
    return {"to": to, "own": OWN}
