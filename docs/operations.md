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

## 観測データ蓄積基盤（tgsvr、2026-08-26 稼働開始）

観測ストア（store/observations.nc + weather.sqlite）の正本は **tgsvr**
（`~/.ssh/config` の `tgsvr`、`~/dev/weather/WeatherStatic/store/`）で管理する。

- 初期データ: 旧 WeatherCore の pg_dump（weather.gz）の jma_daily を
  backfill_daily.py で投入済み（1880-11-01〜2022-03-14、69.1M セル）
- ダンプ末尾 5 日分（2022-03-10〜14）は速報値だったため etrn から `--force`
  再取得して置換済み（値訂正 8 件・品質フラグ更新 5 件・欠測補完 10 件を確認）
- ギャップ（2022-04〜2026-07）は backfill_etrn.py で取得（再開可能・ingest_log 管理）
- 進行中の月は etrn 対象外のため、月初の穴は毎月 2 日の月次 cron が前月分で埋める

tgsvr の crontab（設置済み）:

```cron
# 毎正時+10分: アメダス map JSON + mdrr 確定値CSV（7日窓。速報値は毎回再取得し訂正記録）
10 * * * * cd $HOME/dev/weather/WeatherStatic && flock -w 600 $HOME/dev/weather/store.lock ./.venv/bin/python accumulate.py >> $HOME/dev/weather/logs/accumulate.log 2>&1
# 毎月2日 03:30: 前月分を etrn 確定値で置換（月次の二重チェック）
30 3 2 * * cd $HOME/dev/weather/WeatherStatic && flock -w 10800 $HOME/dev/weather/store.lock ./.venv/bin/python backfill_etrn.py --from $(date -d "-1 month" +\%Y-\%m) --to $(date -d "-1 month" +\%Y-\%m) --force >> $HOME/dev/weather/logs/etrn_monthly.log 2>&1
```

**ロックはスクリプト自身が取る**（2026-08-27 以降）。observations.nc は
「コピー → 更新 → rename」で置き換えるため、同時実行すると後勝ちで書込が失われる。
以前は cron 側の `flock` に頼っていたが、手動実行の手順から簡単に抜け落ちるため
（実際に抜けた）、`weatherlib/storelock.py` を accumulate / backfill_daily /
backfill_etrn が自前で使うようにした。**cron やコマンドラインで flock を書く必要はない**
（書いても二重に効くだけで害はない）。

- ロックファイルは `WeatherStatic/store.lock`（`WEATHER_STORE_LOCK` で変更可）
- accumulate は 40 分待って取れなければ**その回を見送る**（7 日窓なので次回が拾う）。
  見送りはログに 1 行残る
- バックフィルは既定 1 時間待つ。長時間走るので、その間 accumulate は見送られる

## アメダス現況ミラー（tgsvr をオリジンに、Cloudflare プロキシ経由で公開）

**目的**: トップページは訪問者のブラウザから気象庁へ直接 `latest_time.txt` と
`map/{ts}.json`（245KB）を `no-store` で取りに行っていた。つまり**ページビュー
ごとに気象庁へアクセス**していた。ミラーを挟むと、気象庁への取得は **10 分に 1 回**
だけになり、非公式エンドポイントの仕様変更・CORS・障害からもサイトが切り離される。

```cron
# 10分毎: アメダス 10 分値を取得（ミラー + 半月 NetCDF アーカイブ）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_amedas_mirror.py >> $HOME/dev/weather/logs/amedas_mirror.log 2>&1
```

ストアには触らないので `store.lock` とは無関係（accumulate と並行して安全）。

### 公開手順（外部操作はユーザーが行う）

1. Cloudflare で `amedas.time-j.net` を**プロキシ有効**で tgsvr のグローバル IP へ向ける
2. Cloudflare Origin CA 証明書を発行し `/etc/caddy/certs/time-j-net.{crt,key}` に置く
3. `WeatherStatic/deploy/caddy-amedas.conf` を `/etc/caddy/Caddyfile` に追記して
   `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`
4. 疎通確認: `curl -sI https://amedas.time-j.net/latest_time.txt`
5. サイト側を切り替える（**この環境変数を設定するまで従来どおり気象庁を直接見る**）:

```cron
52 * * * *  cd $HOME/dev/weather/WeatherStatic && WEATHER_AMEDAS_BASE=https://amedas.time-j.net ./.venv/bin/python fetch_data.py && ./.venv/bin/python generate.py
```

切り戻しは環境変数を外して再生成するだけ。

### オリジンを晒さないための注意

- caddy-amedas.conf は **Cloudflare の IP 以外を 403** にしている。オリジン IP が
  知られてもプロキシを迂回されない（IP 範囲が変わったら追記すること）
- 80/443 を公開すると **Caddyfile の全 vhost が外から到達可能になる**。
  `kobo.aiseed.page` のように IP 制限のない vhost は、公開前に制限を足すか
  Host が一致しない要求を落とす既定 vhost を用意する

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

## 世界天気（worldtime-web 連携）

```cron
# met.no 予報+METAR → data/world/（時間毎で十分。METARだけ高頻度も可）
40 * * * *   cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_world.py
# 世界気温タイル 1日4回（各ランの公開後 ≈ JST 17:30/23:30/5:30/11:30）
30 17,23,5,11 * * *  cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_tiles.py
```

タイルは conda 環境（../.venv、cfgrib 必要）で実行する点に注意。

## rclone の代替（cf-publish 0.2.0 以降）

R2 への同期は `cf-publish r2 sync` でも可能（rclone 設定不要。
R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / CLOUDFLARE_ACCOUNT_ID を設定）:

```bash
cf-publish r2 sync ~/wxpub/forecast weather-forecast/forecast --delete
cf-publish r2 sync ~/wxpub/charts weather-forecast/charts --delete
cf-publish r2 sync WeatherStatic/dist weather-obs
```

初回は --dry-run で差分を確認してから。実運用実績がつくまでは rclone も併記のまま残す。
