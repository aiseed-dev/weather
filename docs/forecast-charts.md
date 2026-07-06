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
