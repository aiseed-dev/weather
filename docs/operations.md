# 運用手順書（気温と雨量の統計 + 数値予報配信）

2026-07-06 作成。この 1 枚で全システムを運用できることを目的とする。
詳細設計は各設計書（DESIGN.md / forecast-distribution.md / forecast-charts.md /
r2-deployment.md）を参照。

## 日次 cron（WeatherStatic = 気温サイト）

```cron
# 毎正時+10分: アメダス map JSON を蓄積（日平均の材料）
10 * * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python accumulate.py
# 毎時50分: 最新CSV・予報・現在天気 → サイト再生成
52 * * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_data.py && ./.venv/bin/python generate.py
# 日次: 投票集計（Workers+KV 版。要 VOTES_KV_NAMESPACE_ID）
15 1 * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python aggregate_votes.py --kv
```

- サイト生成の環境変数（本番時）: `WEATHER_CHARTS_BASE`（チャート画像の公開URL、
  既定 /charts）、`WEATHER_VOTE_URL`（Worker の vote.gif、既定 /vote.gif）
- デプロイ: `cf-publish public/ --project <名前>`（実績あり: ecitizen.jp）

## 数値予報（00z/12z の 2 回。日本時間 17:30 / 5:30 目安）

```cron
30 17,5 * * *  cd $HOME/dev/weather && \
  ./.venv/bin/python tools/publish_forecast.py --out $HOME/wxpub --tier core && \
  ./.venv/bin/python tools/publish_charts.py --out $HOME/wxpub && \
  rclone sync $HOME/wxpub/forecast r2:weather-forecast/forecast && \
  rclone sync $HOME/wxpub/charts r2:weather-forecast/charts
```

- publish_forecast と publish_charts は grib-cache を別に持つが同じ bulk GRIB。
  帯域が気になる場合は charts の `--out` を forecast と同じにしても安全
- ENS 降水（アンサンブル）は publish_charts の `--ens`（既定オン）

## 過去観測データ（月次で十分）

```bash
cd ~/dev/weather/WeatherStatic
./.venv/bin/python export_dist.py && rclone sync dist/ r2:weather-obs
```

## 障害時・再開

| 症状 | 対処 |
|------|------|
| backfill_etrn が途中で止まった | 同じコマンド再実行（ingest_log で続きから） |
| publisher が途中で止まった | 同じコマンド再実行（manifest で続きから、GRIB もキャッシュ） |
| 予報 office が 404 | jma.py の OFFICE_REMAP 参照（014030→014100 等の統合例外） |
| JMA から 403/429 | しばらく止める。恒常なら jma.py の MIN_INTERVAL を増やす |
| チャートの日本語が豆腐 | Noto CJK フォント（fonts-noto-cjk）を確認 |
| Pages で _headers が効かない | cf-publish 0.1.1 以上を使う（0.1.0 は資産扱いのバグ） |

## まだ手つかず（優先度低）

- アプリ（Flet）の mirror ソースを実 R2 URL で最終確認
- conda-forge 公開（PyPI 公開後に grayskull → staged-recipes）
- ERA5 気候値パック（notebooks/06 を Colab で実行）
- jma_daily の pg_dump が入手できたら backfill_daily.py で 2020 年以前を投入
