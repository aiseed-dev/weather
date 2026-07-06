# Cloudflare R2 への反映手順（過去データ dist/ と 予報パック forecast/）

設計: `docs/forecast-distribution.md`（予報）と WeatherStatic の
`export_dist.py`（過去観測）。**このドキュメントの操作はすべて外部への
接続を伴うため、実行はユーザー自身が行う。**

## 一度だけの準備

### 1. バケット作成（ダッシュボード）

dash.cloudflare.com → R2 → Create bucket。推奨は 2 つに分ける:

| バケット | 中身 | 理由 |
|---|---|---|
| `weather-obs` | WeatherStatic/dist/（過去観測 NetCDF） | 小さく安定。無料枠に収まる |
| `weather-forecast` | publish/forecast/（予報パック） | 大きく回転が速い。容量管理を分離 |

### 2. 公開アクセス

各バケット → Settings → Public access。
- 手軽: **r2.dev サブドメイン**を有効化（`https://pub-xxxx.r2.dev`）
- 本番: **カスタムドメイン**（例 `data.aiseed.dev`）を接続。
  アプリの `mirror_url` はこの URL + `/forecast` になる

### 3. rclone の設定

S3 互換 API トークンを R2 → Manage R2 API Tokens で作成
（権限: Object Read & Write、対象バケットのみ）。`~/.config/rclone/rclone.conf`:

```ini
[r2]
type = s3
provider = Cloudflare
access_key_id = <ACCESS_KEY_ID>
secret_access_key = <SECRET_ACCESS_KEY>
endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

確認: `rclone lsd r2:`

## 運用

### 過去観測データ（WeatherStatic）

```bash
cd ~/dev/weather/WeatherStatic
./.venv/bin/python export_dist.py            # dist/ を再生成
rclone sync dist/ r2:weather-obs --progress  # 差分同期（sha256 は manifest.json に）
```

更新頻度は月 1 回程度で十分（日々の追記は年ファイル・地点ファイルの
再生成で反映される）。

### 予報パック

```bash
cd ~/dev/weather
./.venv/bin/python tools/publish_forecast.py --out ~/wxpub          # 最新ラン
rclone sync ~/wxpub/forecast r2:weather-forecast --progress
```

- `--tier core` なら core のみ（約 3GB/ラン、定番チャートが全部動く）
- 00z は日本時間 15–17 時ごろ、12z は 3–5 時ごろに完全公開される。
  cron 例（core を 1 日 2 回、ext は必要になったら）:

```cron
30 17 * * *  cd $HOME/dev/weather && ./.venv/bin/python tools/publish_forecast.py --out $HOME/wxpub --tier core && rclone sync $HOME/wxpub/forecast r2:weather-forecast
30  5 * * *  cd $HOME/dev/weather && ./.venv/bin/python tools/publish_forecast.py --out $HOME/wxpub --tier core && rclone sync $HOME/wxpub/forecast r2:weather-forecast
```

**sync の順序に注意**: publisher は latest.json を最後に更新するので、
`rclone sync` 一発でも「新ランのファイルが揃う前に latest.json だけ
新しくなる」ことは起きにくいが、厳密にやるなら 2 段に分ける:

```bash
rclone sync ~/wxpub/forecast/runs r2:weather-forecast/runs
rclone copy ~/wxpub/forecast/latest.json r2:weather-forecast
```

### アプリ側（利用者に案内する設定）

```toml
# ~/.config/aiseed-weather/config.toml
forecast_source = "mirror"
mirror_url = "https://<公開URL>/forecast"   # 例 https://data.aiseed.dev/forecast
```

## 容量の目安（実測ベース）

| 構成 | R2 使用量 | 無料枠 10GB |
|---|---|---|
| 過去観測 dist/ 全部 | 現在 0.08GB（10 年でも 1GB 未満） | 余裕 |
| 予報 core × 2 ラン | 約 6GB | 収まる |
| 予報 全部 × 2 ラン | 約 25GB | 超過分 約 $0.22/月 |

配信（egress）は何 GB 出ても恒久無料。課金要素はストレージのみ。
