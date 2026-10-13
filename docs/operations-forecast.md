# 運用手順書（数値予報・過去観測配布・世界天気）

サイト（WeatherStatic）の運用は [operations.md](operations.md)。こちらは
ECMWF 数値予報の配信、観測データの配布、世界天気の取得を扱う。

設計は [forecast-spec.md](forecast-spec.md) /
[forecast-distribution.md](forecast-distribution.md) /
[forecast-charts.md](forecast-charts.md) /
[r2-deployment.md](r2-deployment.md) を参照。

## cron

**cron の設定は運用者が行う。** ここに載せるのは照合用の一覧。
`crontab -l` と見比べて、足りないものだけを足すこと。

```cron
# 数値予報 00z/12z（日本時間 17:30 / 5:30 目安）
30 17,5 * * * cd $HOME/dev/weather && \
  ./.venv/bin/python tools/publish_forecast.py --out $HOME/wxpub --tier core --charts && \
  ./.venv/bin/python tools/publish_charts.py --out $HOME/wxpub --model gfs && \
  ./.venv/bin/cf-publish r2 sync $HOME/wxpub/forecast weather-forecast/forecast --delete && \
  ./.venv/bin/cf-publish r2 sync $HOME/wxpub/charts weather-forecast/charts --delete

# 世界天気（met.no 予報 + METAR）→ data/world/ → public/data/world/（公開は build_site.py の cron が運ぶ）
40 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_world.py >> $HOME/dev/weather/logs/world.log 2>&1

# 世界気温タイル 1 日 4 回（各ランの公開後 ≈ JST 17:30/23:30/5:30/11:30）
30 17,23,5,11 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_tiles.py
```

**deb2 の venv は repo 直下の `../.venv` だけ**（operations.md の一覧と同じ。
`./.venv` は deb2 に無く、書くと毎時 exec 失敗する。2026-10-09 まで一覧がその形だった）。

## 世界天気（worldtime-web 向け）

- 都市マスター `WeatherStatic/master/world_cities.json` は **worldtime-web 側**の
  `tools/export_world_cities.py` が作る。master/ で唯一 git 管理し、
  `sync_to_server.py --apply` で deb2 へ届く。都市を増減したら worldtime 側で
  作り直してコミットし、送る
- 初回だけ deb2 で手動実行して data/world/ を作る:
  `ssh deb2 'cd dev/weather/WeatherStatic && ../.venv/bin/python fetch_world.py'`
- CORS ヘッダ（`/data/world/*`）は generate.py が `_headers` に書く。
  配信確認: `curl -sI https://weather.time-j.net/data/world/index.json | grep -i access-control`
- 利用側は www.time-j.net の都市ページ（現在の天気・天気予報）。予報は日本以外の
  全都市（515）、日本域は意図的に空（worldtime 側 K12）。全量 1 回 約 4 分
  天気地図（map.json）は 2026-10-09 に廃止

## 数値予報

### ディスク（ECMWF の生データはサーバーに溜めない）

| 置き場所 | 中身 | 保持 |
|---|---|---|
| `~/wxpub/grib-cache/{run}/` | publish_forecast の bulk GRIB（150MB × 65） | パック後に削除。core→ext の間だけ最大 約10GB |
| `~/wxpub/grib-cache-charts/{model}/{run}/` | publish_charts の GFS NetCDF・ENS .npy（`--model ecmwf` で描くときは bulk GRIB も） | **ステップを描き終えるたびに削除**、ラン終了時にディレクトリごと削除（2026-10-09 修正。それ以前は 10GB+2GB が残り続け、次のランでも使い回していた） |
| `~/wxpub/forecast/`、`~/wxpub/charts/` | 配信する生成物（R2 へ sync） | forecast は `--runs-keep 2`、charts は最新 1 ラン上書き |
| デスクトップアプリ `~/.cache/aiseed-weather/ecmwf/` | 利用者の端末側のキャッシュ | サーバーには無い |

一時的なピークは publish_forecast の 約10GB。これを下回る空きしか無い環境では
`--tier core` だけにする（約2.7GB）。

### ECMWF の GRIB は 1 回だけ取る（2026-10-13〜）

律速は ECMWF の bulk GRIB の取得（1 ステップ 149MB）で、回線は速いとき 13MB/s、
遅い時間帯は 1〜5MB/s（並列にしても合計は増えない）。そこで:

- `publish_forecast --charts` が、パックを作った同じ GRIB で ECMWF のチャート
  （日本域 10 製品＋海外向けセット）も描く。以前は publish_charts が同じ GRIB を
  もう一度取っていた（取得量が 2 倍）。GFS と ENS は `publish_charts --model gfs`
- 取得は 1 本のスレッドで順に進め、decode・書き・描画は別プロセス（`--workers`、既定 3）で
  並べる。取り終えたステップから処理するので、CPU の時間は取得の陰に隠れる
- 取得が 30 秒止まったら切り、受け取った所から Range で続きを取る（以前は 300 秒待って
  最初から取り直し、1 本 388 秒かかった例がある）。止まらなくても 60 秒の平均が 200KB/s を
  下回る接続は繋ぎ直す。取り切れなかった `.part` は消す
- charts/latest.json は、それぞれが自分の描いたモデルの分だけ書き替える
  （ecmwf は publish_forecast、gfs と ens は publish_charts）

見込み（1 ラン 65 ステップ、ENS・GFS を除く）: 回線 13MB/s で 約 36 分 → 約 13 分、
4MB/s で 約 90 分 → 約 40 分。測定の記録は ~/dev/nc4rs-bench の README。

ENS 降水（アンサンブル）は `publish_charts --ens`（既定オン）。

海外向けセット（worldtime 用、日本の EEZ を空白、861 枚/ラン）も同じ
`publish_charts` が描く（`charts/ecmwf/world/…`）。詳細は forecast-charts.md。
worldtime 側がリンクする公開 URL（R2 の r2.dev か独自ドメイン）は未決。

## 過去観測データの配布（月次で十分）

「過去の気象データ・ダウンロード」型の動的切り出しの代わりに、
**あらかじめ分割した NetCDF を静的配信する**。サーバー側の切り出し処理
（＝運営費の主因）が要らなくなる。

```bash
cd ~/dev/weather/WeatherStatic
./.venv/bin/python export_dist.py
./.venv/bin/cf-publish r2 sync dist weather-obs
```

`export_dist.py` が出すもの:

| 出力 | 内容 |
|------|------|
| `dist/full/observations.nc` | 全部入りスナップショット（一括利用者向け） |
| `dist/stations/{code}.nc` | 1 地点 × 全期間（各数百 KB） |
| `dist/years/{yyyy}.nc` | 全地点 × 1 年（年断面の分析向け） |
| `dist/stations.json` | 地点メタデータ |
| `dist/manifest.json` | ファイル一覧（サイズ・sha256・更新時刻・被覆期間） |

更新は差分で速い。全期間で不変の年ファイル・地点ファイルは、内容ハッシュが
変わらない限り書き直さない。すべての NetCDF に出典（気象庁）と加工者の表示を
グローバル属性で埋め込む（政府標準利用規約 2.0 / CC-BY 4.0 互換の要件）。

## R2 への同期

`cf-publish r2 sync` を使う（rclone 設定は不要）。

```bash
cf-publish r2 sync ~/wxpub/forecast weather-forecast/forecast --delete
cf-publish r2 sync ~/wxpub/charts weather-forecast/charts --delete
cf-publish r2 sync WeatherStatic/dist weather-obs
```

初回は `--dry-run` で差分を確認してから。資格情報は
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_ACCOUNT_ID`。

rclone を使う場合は `rclone sync $HOME/wxpub/forecast r2:weather-forecast/forecast`
と読み替える。

## 障害時・再開

| 症状 | 対処 |
|------|------|
| publisher が途中で止まった | 同じコマンド再実行（manifest で続きから、GRIB もキャッシュ） |
| チャートの日本語が豆腐 | Noto CJK フォント（fonts-noto-cjk）を確認 |
| タイルが cfgrib で落ちる | conda 環境（`../.venv`）で実行しているか確認 |

## まだ手つかず

- アプリ（Flet）の mirror ソースを実 R2 URL で最終確認
- conda-forge 公開（PyPI 公開後に grayskull → staged-recipes）
- ERA5 気候値パック（notebooks/06 を Colab で実行）
