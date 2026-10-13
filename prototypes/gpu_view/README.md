# GPU 描画の試作（Windy 並みの動く地図）

アプリ本体（`src/`）とは別の試作。Flet の拡張として、Flutter 側で GPU を使って描く部品
`flet-gpufield` を作り、Python から格子データを渡して動かす。

- 色の場: 格子データを 16 bit に詰めて GPU に渡し、シェーダー（`shaders/field.frag`）が
  画面の画素ごとに双一次補間して配色する。配色はアプリの図と同じ（T2M・WIND10M）
- 風の粒子: Dart で動かし、直近 12 コマの位置を細い三角形の列にして 1 回で描く（drawVertices）
- 拡大（ホイール・ピンチ）・移動（ドラッグ）・読み取り（マウスを置くと経緯度・気温・風速）は
  部品の中で完結する。Python は時刻や層を変えたときにだけデータを渡す
- 出典は図の右下に必ず出す

## 測定（2026-10-13、debpc: Intel AlderLake-S GT1 内蔵 GPU、Linux、ウィンドウ約 1500×1000）

| 描き方 | 粒子 | fps | 描画（raster） |
|---|---|---|---|
| 色の場だけ | 0 | 60 | 1 ms |
| ＋海岸線 | 0 | 60 | 3 ms |
| 尾を画像に焼いて重ねる（Windy 型） | 6,000 | 16〜20 | 50 ms |
| 尾を線で描く（drawRawPoints） | 6,000 / 12,000 | 47 / 24 | 21 / 41 ms |
| 尾を三角形で描く（drawVertices、既定） | 6,000 / 12,000 / 20,000 | 59 / 60 / 50 | 7 / 11 / 18 ms |
| 既定（三角形、尾 12 コマ） | 10,000 | 56〜58 | 12 ms |
| 既定で時刻を 0.2 秒ごとに進める | 10,000 | 45 | 12 ms（切り替え 1 回 約 9 ms） |

- 粒子の移動の計算は 1 コマ 0.3〜0.5 ms（Dart の AOT）。詰まるのは描画側だった
- 線を太さ付きで描くと、線を三角形に分ける手間が描画スレッドの CPU に乗る。最初から三角形に
  して渡すと GPU だけで済む
- データの準備: 必要な 3 通報だけを Range で取り（1 ステップ約 2.6MB）、eccodes で直接読むと
  1 ステップ 43 ms（アプリの cfgrib は 1 回開くだけで 0.2 秒以上）

## 作り方と動かし方

```
cd prototypes/gpu_view
../../.venv/bin/python prepare_data.py --run 2026101212 --steps 0-72/3
mkdir -p app/src/data/2026101212 && cp data/2026101212/*.bin data/2026101212/meta.json app/src/data/2026101212/
cd app
../../../.venv/bin/flet build linux      # build/linux/gpu-view-prototype
../../../.venv/bin/flet build web        # build/web（flet serve build/web）
```

自作の部品は普段の `flet run`（出来合いの実行環境）では表示されない。`flet build` で
アプリごと作る（Linux 約 50 秒）。`[tool.flet.dev_packages]` が手元の `flet-gpufield` を入れる。

測定用の環境変数: `GPUVIEW_STATS=ファイル`（速さの報告を書き出す）、`GPUVIEW_PARTICLES`、
`GPUVIEW_COAST=0`、`GPUVIEW_TRAIL=vertices|history|hairline|image`、`GPUVIEW_PLAY=秒`。

## 残り

- アプリの地図画面への組み込み（データはアプリのキャッシュの GRIB か配布パックから）
- 時刻の切り替えを速く: 全ステップを先に渡し、Dart 側で番号だけ切り替える
- 等圧線・気圧面の層、極域などの投影、全球の範囲
- ブラウザー版（Web）は描画は動くが、同じ内容で 1 コマの組み立てが重い（JavaScript）
