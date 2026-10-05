#!/usr/bin/env bash
# 生成済みサイトを LAN に配信するレビュー用サーバー。
#
# 実データ（store/・master/・data/）は deb2 にしか無いため、実データでの見た目の
# 確認は deb2 側で生成して、手元のブラウザから見に行く（手元ではテストデータ）。
#
#   ./serve_preview.sh              … public/ をポート 8765 で配信
#   ./serve_preview.sh --build      … fetch_data + generate してから配信
#   ./serve_preview.sh --build-only … 生成だけして終わる（サーバーは触らない）
#   PORT=9000 ./serve_preview.sh    … ポート変更
#
# 既に同じポートで動いていれば古いプロセスを止めてから立て直す。
# 静的ファイルを読ませるだけなので、LAN 外へは公開しないこと。
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8765}"
PY=../.venv/bin/python     # venv はリポジトリ直下に 1 つ
BUILD=0
SERVE=1

for arg in "$@"; do
    case "$arg" in
        --build)      BUILD=1 ;;
        --build-only) BUILD=1; SERVE=0 ;;
        *) echo "不明な引数: $arg" >&2; exit 1 ;;
    esac
done

if [ "$BUILD" = 1 ]; then
    echo "== データ取得 =="
    $PY fetch_data.py
    echo "== サイト生成 =="
    $PY generate.py
fi

[ "$SERVE" = 1 ] || exit 0

if [ ! -d public ]; then
    echo "public/ がありません。--build を付けて実行してください。" >&2
    exit 1
fi

# 同じポートの残骸を掃除（--directory public を目印にする）
pkill -f "http.server $PORT --bind" 2>/dev/null || true
sleep 0.5

mkdir -p logs
setsid nohup $PY -m http.server "$PORT" --bind 0.0.0.0 --directory public \
    > logs/preview-server.log 2>&1 < /dev/null &
sleep 1

if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    echo "配信中: ポート $PORT / $(find public -name '*.html' | wc -l) ページ"
    for ip in $(hostname -I); do
        case "$ip" in 192.*|10.*|172.*) echo "  http://$ip:$PORT/" ;; esac
    done
    echo "停止: pkill -f 'http.server $PORT'"
else
    echo "起動に失敗しました。logs/preview-server.log を確認してください。" >&2
    exit 1
fi
