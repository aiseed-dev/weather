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
  ./.venv/bin/python tools/publish_forecast.py --out $HOME/wxpub --tier core && \
  ./.venv/bin/python tools/publish_charts.py --out $HOME/wxpub && \
  ./.venv/bin/cf-publish r2 sync $HOME/wxpub/forecast weather-forecast/forecast --delete && \
  ./.venv/bin/cf-publish r2 sync $HOME/wxpub/charts weather-forecast/charts --delete

# 世界天気（met.no 予報 + METAR）→ data/world/
40 * * * * cd $HOME/dev/weather/WeatherStatic && ./.venv/bin/python fetch_world.py

# 世界気温タイル 1 日 4 回（各ランの公開後 ≈ JST 17:30/23:30/5:30/11:30）
30 17,23,5,11 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_tiles.py
```

**タイルだけ conda 環境（`../.venv`）で実行する。** cfgrib が要るため。
他は WeatherStatic の venv。

## 数値予報

`publish_forecast` と `publish_charts` は grib-cache を別に持つが、参照する
bulk GRIB は同じ。帯域が気になる場合は charts の `--out` を forecast と
同じにしてよい（安全）。

ENS 降水（アンサンブル）は `publish_charts --ens`（既定オン）。

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
