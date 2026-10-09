# 数値予報チャート（旧 Gfs セクションの再構築）設計書

作成: 2026-07-06。ステータス: **承認済み**（ユーザー決定: 製品セットは拡充・
特に雨量にアンサンブル、既定モデルは ECMWF、極域は北極のみ、画像は大きく見やすく）。

## 目的

旧 creativeweb.jp の Gfs セクション（GFS の事前レンダリング画像を JS ステップ
送りで表示）を静的サイトに再構築する。ユーザー提案により **ECMWF / GFS の
モデル選択**を付ける。既定は ECMWF（IFS）— 検証スコアで GFS より一貫して
上のモデルを既定にする。GFS は比較用に残す（2 モデルの割れ方は予報の
不確実性のシグナルで、旧サイトにない新価値）。

## 全体像

```
[サーバー処理（ユーザー実行・publisher と同居）]        [静的サイト]
tools/publish_charts.py                             /Forecast/ ページ
  ECMWF: GCSミラー bulk GET（publish_forecast と共用）→   モデル切替 (ECMWF/GFS)
  GFS:   AWS noaa-gfs-bdp-pds .idx + Range 取得    →   製品タブ + ステップスライダー
  figures/ で PNG レンダリング（既存コード）        →   <img> 差し替え + manifest.json
  charts/{model}/{product}/{step}.png + manifest
```

## 1. データ取得

| モデル | 取得元 | 方式 |
|--------|--------|------|
| ECMWF IFS 0.25° | GCS ミラー（実測済み・publisher と同じ URL） | bulk GET（150MB/步）。**publish_forecast.py の grib-cache を共用**し、同時運用なら追加取得ゼロ |
| GFS 0.25° | AWS `noaa-gfs-bdp-pds`（匿名・.idx あり） | `.idx` から必要フィールドの byte range だけ GET（ECMWF で実証済みの手法。必要 8 フィールド ≈ 数 MB/步） |

- ステップ: 3 時間刻み 0–144h ＋ 6 時間刻き 150–240h（両モデル共通で揃える）
- ラン: 00z / 12z の 2 回（publisher と同じ推奨運用）

## 2. 製品セット（旧サイトの構成を踏襲し、figures/ の実装済みレンダラで賄う）

| product | 内容 | レンダラ | 領域 | モデル |
|---------|------|----------|------|--------|
| msl-precip | 海面気圧＋降水 | msl + tp overlay | 日本周辺 | 両方 |
| t2m | 地上気温 | t2m_chart | 日本周辺 | 両方 |
| t850 / t500 / t925 | 気圧面気温 | _scalar_chart (t@lv) | 日本周辺 | 両方 |
| wind10m | 地上風 | wind_chart | 日本周辺 | 両方 |
| wind300 / wind500 | 気圧面風（ジェット） | wind_chart | 日本周辺 | 両方 |
| t500-polar / t850-polar | 極域気温（旧 TemperaturePoler） | 極域再投影（実装済み ARCTIC） | 北極域 | 両方 |
| **ens-tp-mean** | ENS 24時間降水量（アンサンブル平均） | 新規 ens_precip | 日本周辺 | ECMWF ENS |
| **ens-tp-prob1** | 24時間降水 1mm 以上の確率 | 新規 ens_precip | 日本周辺 | ECMWF ENS |
| **ens-tp-prob30** | 24時間降水 30mm 以上の確率（大雨） | 新規 ens_precip | 日本周辺 | ECMWF ENS |

- ユーザー決定「雨量にアンサンブル」: ECMWF ENS（enfo, 51 メンバー）の tp を
  .index + Range でメンバー全員分取得し、24 時間窓（T+24, 48, …, 240 の 10 窓）で
  平均と閾値超過確率を計算して描く
- 決定論 10 製品 × 65 ステップ × 2 モデル + ENS 3 製品 × 10 窓
  ≈ **1,330 枚/ラン**
- **画像サイズはユーザー決定で大きく**: 決定論チャート幅 1280px 目安
  （アプリ内表示より大きい。1 枚 100–250KB 見込み）→ 1 ラン合計 ≈ 200MB 前後
- 出力: `charts/{model}/{run}/{product}/{step:03d}.png` ＋ `charts/latest.json`
  （モデルごとの最新ラン・ステップ一覧・製品一覧）
- 保持: 各モデル最新 1 ラン（画像はパックと違い蓄積しない）
- すべての画像に出典と run を焼き込み（figures/footer 実装済み。
  ECMWF: CC-BY-4.0 / GFS: NOAA パブリックドメイン）

## 3. サイト側（WeatherStatic）

- `/Forecast/`（ナビの「天気図」を差し替え）: 1 ページ構成
  - セグメント切替: **ECMWF / GFS**（既定 ECMWF）
  - 製品タブ（msl-precip / 気温×3 / 風×3 / 極域×2）
  - ステップスライダー＋再生ボタン（旧サイトの JS 送りを современ化。
    `<img>` の src 差し替えのみ、依存ライブラリなし）
  - latest.json を 1 回 fetch して選択肢を構成（ダッシュボード方式）
- 画像の置き場所: **R2**（1 ラン 100MB × 日 2 回の回転は Pages の
  デプロイ単位に向かない。サイト HTML は Pages、画像は R2 と役割分担）
- 旧 URL (/Gfs/...) は /Forecast/ へのリダイレクト（_redirects）

## 4. 実装順（マイルストーン）

| # | 内容 | 検証 |
|---|------|------|
| C1 | GFS 取得（.idx + Range、日本域 8 フィールド） | 実ランで数 MB/步を確認、cfgrib decode |
| C2 | publish_charts.py: 両モデル → PNG 一式 + latest.json | 1 ラン分生成、代表画像の目視 |
| C3 | /Forecast/ ページ（モデル切替・タブ・スライダー） | ローカル配信でブラウザ検証 |
| C4 | R2 反映手順を docs/r2-deployment.md に追記 | — |

## 5. 確認したい点

1. 製品セットは上の 9 つでよいか（旧サイト同等＋wind500。追加希望があれば）
2. 既定モデルは ECMWF でよいか（GFS を既定にもできる）
3. 極域は北極のみでよいか（旧サイトに南極はなかった認識）

## 海外向けセット（2026-10-09 追加、time-j.net worldtime 用）

worldtime（世界時計）の都市ページから、その都市の地域の天気図へ導線を張るための
セット。**日本の排他的経済水域（EEZ）を空白にして描く**。日本向けの予報を
Web に出さないという線引き（worldtime 設計書 K12/K13）を図でも守るため。

| 項目 | 内容 |
|---|---|
| 領域 | 全球、アジア、ヨーロッパ、アフリカ、北米、南米、オセアニア（figures/regions.py に追加） |
| 製品 | msl-precip、t2m、wind10m |
| モデル | ECMWF のみ（GFS 比較は日本周辺セットだけ） |
| ステップ | 0–240h の 6 時間刻み（41） → 7 × 3 × 41 = **861 枚/ラン** |
| 出力 | `charts/ecmwf/world/{region}/{product}/{step:03d}.png`、`latest.json` の `models.ecmwf.world` |
| 空白 | `figures/_blank.py`（`jp_eez`）。日本の EEZ ＋ 重複主張域（千島・尖閣・竹島）＋ 日韓共同開発区域をすべて含める（安全側、ユーザー決定）。境界の外側に 1 格子（0.25°）の余白 |
| 元データ | Marine Regions World EEZ v12（Flanders Marine Institute、CC BY 4.0）。`figures/_jp_eez_claims.geojson` に間引いて同梱（出典・ライセンスはファイル内） |

仕組み: 全描画経路が最後に `_coastlines.apply_coastlines` を通るので、そこで
「有効中の空白マスク」を塗ってから海岸線を乗せる。有効化は
`with blanking("jp_eez"):`（publish_charts.render_world だけが使う。アプリは使わない）。
**マスクの無い領域で空白を有効にすると描画は例外で止まる**。空白なしの図が黙って出ることはない。

前計算（領域を増やしたとき・EEZ を更新したとき）:

```bash
PYTHONPATH=src python -m aiseed_weather.figures._precompute_coastlines   # 海岸線・陸（cartopy 無しなら pyshp + ~/.cache の Natural Earth）
PYTHONPATH=src python -m aiseed_weather.figures._precompute_eez_blank    # 空白マスク
```
