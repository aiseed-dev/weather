# 運用手順書（気温と雨量の統計 + 数値予報配信）

2026-07-06 作成。この 1 枚で全システムを運用できることを目的とする。
詳細設計は各設計書（DESIGN.md / forecast-distribution.md / forecast-charts.md /
r2-deployment.md）を参照。

## 日次 cron（WeatherStatic = 気温サイト）

```cron
# 10分毎: アメダス地点別を集める（エリアごとに Worker を呼ぶ。取り寄せはしない）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_points.py >> $HOME/dev/weather/logs/points.log 2>&1
# 日次 1:30: 統計の蓄積。前日ぶんの毎正時 map JSON と確定値 CSV から nc を作る
30 1 * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python accumulate.py
# 10分毎: 現在値だけ更新（トップページが読む public/data/current.json）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_data.py --current-only >> $HOME/dev/weather/logs/current.log 2>&1
# 10分毎: 実況ページ（気温・風・Python グラフ工房）。fetch_points の後に回す
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && sleep 90 && ./.venv/bin/python generate_status.py >> $HOME/dev/weather/logs/status.log 2>&1
# 毎時50分: 最新CSV・予報・現在天気 → サイト再生成
52 * * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_data.py && ./.venv/bin/python generate.py
# 日次: 投票集計（Workers+KV 版。要 VOTES_KV_NAMESPACE_ID）
15 1 * * *  cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python aggregate_votes.py --kv
```

### 速報と統計を分ける

10 分値（地点別）は**実況ページ用の速報**で、統計には使わない。統計は
`accumulate.py` が毎正時の map JSON と確定値 CSV だけから `observations.nc`
を作る。分けておくと、10 分値の収集が数日止まっても統計は壊れない
（2026-08-29 に実際に 2 日止まり、実況ページだけが古いまま公開された）。

| 経路 | 取得者 | 取得元 | 頻度 | 使い道 |
|------|--------|--------|------|--------|
| 速報 | Worker → R2 → tgsvr | 地点別 10 分値 | 10 分ごと | 実況ページ |
| 統計 | **tgsvr が直接** | map JSON 毎正時 ＋ 確定値 CSV | 1 日 1 回（1 時以降） | observations.nc |

統計は Worker も R2 も経由しない。1,286 地点を並列に取る必要があるのは
10 分値だけで、毎正時の map は全地点 1 ファイル・1 日 24 本しかないため、
tgsvr から直に取れば済む。経路が短いほど壊れる箇所が少ない。

気象庁へのリクエストは 1 日あたり:

| | 回数 |
|---|---|
| 地点別 10 分値（Worker が実行） | 1,286 × 144 |
| map（速報の補完・積雪と天気） | 144 |
| map（統計の時別値） | 24 |
| 確定値 CSV | 4〜6 |

### 確定値 CSV をいつ取りに行くか

気象庁の更新は 1 日 1 巡（[更新時刻](https://www.data.jma.go.jp/stats/data/mdrr/man/update_k.html)）。

| 時刻 | 内容 |
|------|------|
| 1 時頃 | 10 分〜日ごとの値・前日までの順位値 |
| 2 時頃 | 官署の速報値（前日まで） |
| 3 時頃 | アメダスの速報値 |
| 14 時頃 | 官署の確定値 |

`accumulate.py` は **1 日 1 回、7 日窓をまるごと**さらう。更新が 1 日 1 巡
なので毎時は無駄だが、窓を狭めると数日止まったときに埋まらなくなる
（前々日と 7 日前だけに絞ると、8 日以上止まった日は永久に欠ける）。

- 確定値 CSV … 前日〜7 日前 × 2 要素 = 14 リクエスト
- map 毎正時 … 7 日 × 24 = 168 本（取得済みでも更新回が変われば取り直す）

**区切りは日付ではなく直近の 1 時**。日付で区切ると、0 時台に走った回が
「その日ぶん」を消化してしまい、1 時の更新で入った値が翌日まで取り込まれない。
0 時台の実行は前日 1 時の区切りに属させる。

値が変わっていれば `correction_log` に残る。

### 実況ページ（気象庁より高頻度）

気象庁は元データが 10 分値なのに「気温の状況」「風の状況」を**毎時 50 分頃**しか
更新していない。10 分値を持っているので、そのまま 6 倍の頻度で出せる。

| ページ | 内容 |
|---|---|
| `/Status/Temperature/` | 気温の順位・平年差・当日 10 分値グラフ（サーバー生成 SVG） |
| `/Status/Wind/` | 風速の順位・当日最大・風向分布。**最大値は 10 分値ベース**で、気象庁の日最大瞬間風速とは別物 |
| `/Status/Lab/` | ブラウザ内 Python（Pyodide）が `weatherlib/svgchart.py` で描く。起動はボタン押下時のみ |

`generate_status.py` はサイト全体（1,840 ページ）を作り直さず、この 3 ページと
`public/data/amedas-today.json`（916 地点 × 10 分値、gzip 後 61KB）だけを書く。

**ブラウザから気象庁へは取りに行かない**（2026-08-27 以降）。以前のトップページは
訪問者ごとに `map/{ts}.json`（245KB）と推計気象分布のタイル（512px PNG 複数）を
気象庁から直接取得し、ブラウザ内でピクセル判定して天気を出していた。同じ計算は
`fetch_data.py` が既に行っているので、ページは `public/data/current.json`（4KB）
だけを読む。気象庁への取得はサーバー側の 10 分に 1 回に集約される。

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

## アメダス 10 分値の収集とアーカイブ

10 分値は**約 10 日で気象庁から消える**（実測: 10 日前 200 / 11 日前 404）。
etrn から後追いできるのは日別の気温と降水だけで、湿度・気圧・視程・風・10 分降水は
二度と取れない。取り逃しが回復不能なので、収集は tgsvr の死活から切り離す。

### 役割分担

| | 担当 | 内容 |
|---|---|---|
| 収集 | **Cloudflare Worker** | 10 分ごとに気象庁から取得し、**加工せずそのまま** R2 へ置く |
| 配信 | **R2** | 独自ドメインで公開（egress 無料）。Worker は配信に使わない |
| 封入 | **tgsvr** | R2 から拾って半月 NetCDF へ。netCDF4/numpy を使う処理はすべてここ |

Worker を配信に使わないのは、無料枠の **10 万リクエスト/日**を訪問者が食い潰すため。
重い処理を Worker に載せないのは、無料枠の **CPU 10ms・メモリ 128MB** に収まらないため。
この分担なら **tgsvr を公開する必要がない**（アウトバウンドのみ）。

### デプロイ（cf-publish を使う。wrangler は使わない）

| 対象 | コマンド |
|---|---|
| サイト | `cf-publish public/ --project <名前>` |
| アメダスミラー | `cf-publish r2 sync WeatherStatic/public_amedas weather-amedas` |
| 予報パック | `cf-publish r2 sync ~/wxpub/forecast weather-forecast/forecast --delete` |

R2 バケットの作成と独自ドメイン（例 `amedas.time-j.net`）の割当は
ダッシュボードで行う。CORS もバケット側の設定。

**収集 Worker（`workers/amedas/`）は cf-publish の対象外**（Pages 配信と
R2 同期のみ）。Worker を使わない場合は、tgsvr の `fetch_amedas_mirror.py` が
`AMEDAS_R2_BASE` 未設定なら気象庁から直接取るので、そのまま動く。
取得結果を R2 へ流すのは上表の `r2 sync` で足りる。

- **Worker を使う利点**: 収集が tgsvr の死活から独立する（10 分値は 10 日で
  消えるため、取り逃しが回復不能）
- **Worker を使わない場合**: tgsvr が止まっている間の 10 分値は失われる。
  ただし日別値は etrn から後追いできる

### tgsvr 側の cron

```cron
# 10分毎: R2 から拾って NetCDF へ封入（AMEDAS_R2_BASE 未設定なら気象庁から直接取る）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && AMEDAS_R2_BASE=https://amedas.time-j.net ./.venv/bin/python fetch_amedas_mirror.py >> $HOME/dev/weather/logs/amedas_mirror.log 2>&1
```

ストアには触らないので `store.lock` とは無関係（accumulate と並行して安全）。

**R2 に無いスロットは気象庁へ退避する。** Worker が黙って止まっていた場合に
欠測が確定してしまうのを防ぐため。退避した件数はログに出るので、
`うち気象庁へ退避` が恒常的に出るなら Worker が動いていない合図。

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
