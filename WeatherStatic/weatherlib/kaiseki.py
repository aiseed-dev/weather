"""自前のアクセス解析 kaiseki（aiai-pro の koukoku/kaiseki）をページに入れるかどうか。

環境変数 WEATHER_KAISEKI_TO に受け口の URL（例: https://analytics.aiseed.dev）があるときだけ、
全ページの </head> の前にスクリプトを入れ、/kaiseki.js と知らせのページ /kaiseki/ を置く。
無ければ何も入れず、前に置いたものは消す。入れるか（公開するか）は運用者が決める。

kaiseki.js は aiai-pro の koukoku/kaiseki/kaiseki.js の写し（assets/kaiseki.js、aiai-pro 9557e06）。
直すときは aiai-pro 側で直して写し直す。ページを見た人の記録を受け口へ送るので、電気通信事業法の
外部送信規律の対象。送る情報・送り先・目的は /kaiseki/（templates/kaiseki.html）に書く。
"""
from __future__ import annotations

import os

# ID を引き継ぐ自分のサイトのドメイン（kaiseki.js の data-own）
OWN = "time-j.net aiseed.dev"


def settings() -> dict | None:
    """{"to": 受け口, "own": ドメイン} か、入れないなら None。"""
    to = os.environ.get("WEATHER_KAISEKI_TO", "").strip().rstrip("/")
    if not to:
        return None
    return {"to": to, "own": OWN}
