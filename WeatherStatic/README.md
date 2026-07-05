# WeatherStatic

WeatherCore（ASP.NET Core 2.2）を **静的サイト + Python 定期生成** へ移行するための土台。
現状は **パイロット**：`/Temperature/HighsMain`（今日の最高気温・主要都市）を 1 ページ通しで再現。

## アーキテクチャ

```
Python（ローカル・cron 定期実行）
  ├─ [取得層]  PostgreSQL / GCP Datastore / 気象庁  →  data/*.json   ★未実装（実データ環境が必要）
  └─ [描画層]  generate.py + Jinja2 テンプレート     →  public/*.html ★このリポジトリで実装済み
静的ホスティング（public/ をそのまま配信）
```

- **取得層と描画層を分離**。元 C# が表示時に行っていた「キャッシュJSON＋予報のマージ」等の計算は
  取得層（Python）に寄せ、描画層は完成した JSON を描画するだけにする。
- 日付依存の分岐（夏/冬・今日/昨日・季節）は生成時に確定して JSON／コンテキストに焼き込む。

## ディレクトリ

```
generate.py            描画層のビルドドライバ
weatherlib/
  filters.py           ViewUtility/Jma の移植（ondo=気温表示, bcolor=平年差の色, jikan=時刻表示, weather_img）
  season.py            Jma.IsSummer / IsSeason の移植
templates/
  _layout.html         共有レイアウト（_LayoutBootstrap + A を統合）
  partials/_navbar.html
  temperature/highsmain.html
data/
  highsmain.sample.json  合成サンプル（実データが無い環境用）
public/                出力（HTML＋wwwrootからコピーしたCSS/JS/画像）
DATA_CONTRACT.md       取得層が出力すべき JSON の仕様
```

## 使い方

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install jinja2 netCDF4 numpy

# 初回のみ（マスター構築）
python fetch_station_codes.py         # etrn 地点番号一覧 → master/station_codes.json
python build_master.py                # 地点マスタ → master/stations.json
python build_master.py --normals      # 平年値 → master/normals/{code}.json（要 master/raw/*.zip）

# 定期実行（cron）
python fetch_data.py                  # 現在値 → data/today.csv + today_meta.json + forecast.json
python accumulate.py                  # 履歴蓄積 → store/observations.nc（7 日窓）
python generate.py                    # HTML 生成 → public/
python aggregate_votes.py <access.log>  # 服装投票の集計（配信サーバーのログから。1日1回程度）

# 過去データのバックフィル（旧 PostgreSQL の jma_daily から。一度だけ）
#   Windows 側: pg_dump -t jma_daily --data-only weather > jma_daily.dump
python backfill_daily.py jma_daily.dump   # 地点コード=code 同一体系・×10 整数のため無変換で取込

# 不足期間の機械取得（etrn 日別値。再開可能・夜間バッチ向け）
python backfill_etrn.py --from 2025-01 --to 2026-06 --limit 500   # cron で毎晩 500 ページずつ

# プレビュー（静的配信）
python -m http.server 5099 --directory public
#  → http://localhost:5099/Temperature/HighsMain/
```

検証済み（2026-07-05）: accumulate.py の自前計算（日平均=毎正時 24 回平均、日降水量=1 時間値合計）が
etrn の公式日別値と**完全一致**（東京 7/1〜7/4 の平均/最高/最低/降水量で突合）。

## 実データを差し込むには

1. 取得層スクリプトを書き、[DATA_CONTRACT.md](DATA_CONTRACT.md) 準拠の `data/highsmain.json` を出力する。
   - 入力元は元 C# と同じ：キャッシュ `Highs/maindata.json`(夏)/`Highs/wmaindata.json`(冬)
     ＋ Datastore `forecastSummaries`。これらをマージして完成 JSON にする。
2. `python generate.py --data data/highsmain.json` で HTML 生成。
3. cron 等で「取得層 → generate.py」を定期実行し、`public/` を配信/デプロイ。

## 未対応・今後

- モバイル専用レイアウト（元 `_LayoutBootstrapA.NonPC`）は未移植。
  Bootstrap のレスポンシブで一本化するか、別 HTML を生成するかは要判断。
- 残りのページ群（Home / 各地 / ランキング / 月別 / 雨温図 / 降水量 / 天気図）は
  この HighsMain と同じ型で順次移植する。DATA_CONTRACT.md 末尾の表を参照。
- 広告（AdSense）・アクセス解析タグは元レイアウトから必要に応じて復元する。
