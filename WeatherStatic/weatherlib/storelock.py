"""観測ストアの排他ロック。

store/observations.nc を書くジョブ（accumulate / backfill_daily / backfill_etrn）は
「コピー → 更新 → rename」で置き換えるため、同時に走ると**後から rename した側が
勝ち、もう一方の書き込みが黙って消える**。

以前は cron 側の `flock` に頼っていたが、手動実行やドキュメントの写し漏れで簡単に
外れる（実際に外れた）。呼び出し側の作法に依存させず、スクリプト自身がロックを
取るようにする。

使い方:
    from weatherlib.storelock import store_lock

    with store_lock():          # 取れるまで待つ（既定 1 時間）
        ...ストアを書く処理...

タイムアウトすると TimeoutError を投げる。cron から複数ジョブが重なったときは
待たせるのが正しい（捨てると欠測になる）ので、既定は「長めに待つ」。
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_TIMEOUT = 3600.0
POLL_INTERVAL = 1.0

# 既定はリポジトリ直下（WeatherStatic/store.lock）。環境変数で差し替え可能。
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "store.lock"


def lock_path() -> Path:
    return Path(os.environ.get("WEATHER_STORE_LOCK", _DEFAULT_PATH))


@contextmanager
def store_lock(timeout: float = DEFAULT_TIMEOUT, *, log=None, path: Path | None = None):
    """observations.nc を書く区間を直列化する。

    log に呼び出し可能を渡すと、待たされたときだけ 1 行報告する
    （待ち時間が見えないと「固まった」と誤解されるため）。
    path を渡すと別の区間のロックとして使える（build_site.py の生成と公開など）。
    """
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    start = time.monotonic()
    waited = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    raise TimeoutError(
                        f"ロック {path} を {timeout:.0f} 秒待っても取得できなかった"
                    ) from exc
                if not waited and log:
                    log(f"ロック待ち（他のジョブが実行中）: {path}")
                    waited = True
                time.sleep(POLL_INTERVAL)
        if waited and log:
            log(f"ロックを取得（{time.monotonic() - start:.0f} 秒待機）")
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
