#!/bin/bash
# 動く地図（flet-gpufield）入りの Flet 実行環境を build/linux に作る。
#
# 自作の部品は、Flet の出来合いの実行環境では表示されない。一度これを実行しておくと、
# プロジェクトのフォルダーで `flet run` したときに Flet がこの実行環境を使う
# （Linux の Flet は ./build/linux の実行ファイルを先に探す）。部品（packages/flet-gpufield の
# Dart・シェーダー）を変えたら作り直す。約 1 分。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
cd client
"$ROOT/.venv/bin/flet" build linux --yes -o "$ROOT/build/linux" "$@"
echo "作りました: $ROOT/build/linux"
