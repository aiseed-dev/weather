# Weather 静的サイト移行 設計書

- 対象: WeatherCore（ASP.NET Core 2.2 / creativeweb.jp 気温と雨量の統計）
- 移行先: **静的サイト + Python によるローカル定期実行（JSON 生成 → HTML 生成）**
- 作成日: 2026-07-05
- 関連文書: [DATA_CONTRACT.md](DATA_CONTRACT.md)（JSON スキーマ）, [README.md](README.md)（使い方）

---

## 1. 目的と方針

### 1.1 目的
- サポート終了済みの ASP.NET Core 2.2 サーバー常駐構成をやめ、**配信は静的ファイルのみ**にする。
- データ更新は **ローカルの Python スクリプトを cron で定期実行**して行う。
- Windows 固有の依存（`D:\Cache\Weather`）、GCP Datastore、PostgreSQL 常駐への依存を解消する。

### 1.2 基本方針
1. **二層分離** — 「取得層 fetch（データ→JSON）」と「描画層 generate（JSON→HTML）」を完全に分ける。
   境界は [DATA_CONTRACT.md](DATA_CONTRACT.md) の JSON スキーマ。
2. **表示時計算の生成時移動** — 旧サイトがリクエスト毎に行っていた計算
   （予報とのマージ、夏/冬レイアウト判定、今日/昨日の文言分岐）はすべて**生成時に確定**する。
3. **データソースの一次化** — 旧構成の中間層（Datastore・キャッシュ生成バッチ・PostgreSQL）を
   経由せず、**気象庁の公開データから直接取得**する。
4. **忠実な再現** — URL 構造・ページの見た目・表示ロジック（色分け・温度表記）は旧サイトを踏襲する。

---

## 2. 現行システムの構成と課題

```
[現行]
  外部バッチ(所在不明) ──▶ D:\Cache\Weather\*.json ─┐
  気象庁XML/JSON ──▶ GCP Datastore(timej-172810) ──┤──▶ ASP.NET Core ──▶ HTML
  気象庁データ ──▶ PostgreSQL(weather DB) ─────────┘   (リクエスト毎に描画)
```

| 課題 | 内容 |
|------|------|
| ランタイム EOL | ASP.NET Core 2.2 / `Microsoft.AspNetCore.All` はサポート終了 |
| 中間層依存 | キャッシュ JSON を作る外部バッチがリポジトリに無い（ブラックボックス） |
| クラウド依存 | 予報データが GCP Datastore 経由。Datastore を埋める別プロセスも必要 |
| 常駐必須 | DB クエリを行うページがあるため PostgreSQL とアプリの常駐が必要 |

---

## 3. 新アーキテクチャ

```
[新構成]  ※配信サーバーには静的ファイルのみ置く

  ┌──────────────────── Python（ローカル / cron 定期実行）────────────────────┐
  │                                                                            │
  │  fetch_data.py（取得層）                                                   │
  │    ├─ 気象庁 最新気象データCSV ──┐                                        │
  │    ├─ 気象庁 予報JSON API ───────┤─▶ マージ・集計 ─▶ data/*.json          │
  │    ├─ 平年値マスター(初回のみ取得)┤                                        │
  │    └─ 履歴ストア(SQLite, 日次蓄積)┘                                        │
  │                                                                            │
  │  generate.py（描画層）                                                     │
  │    └─ data/*.json + templates/(Jinja2) ─▶ public/*.html                    │
  │                                                                            │
  └────────────────────────────┬───────────────────────────────────────────────┘
                               ▼ rsync / デプロイ
                     静的ホスティング（public/ をそのまま配信）
                       HTML + CSS + JS + 画像 + JSON
```

### 3.1 リポジトリ構成

```
WeatherStatic/
  fetch_data.py          # 取得層ドライバ: 表示用 data/*.json を生成（cron から実行）★実装済
  accumulate.py          # 取得層ドライバ: 履歴蓄積（map JSON 毎正時＋確定値 CSV、7 日窓）★実装済
  generate.py            # 描画層ドライバ（fetch 後に実行）★実装済
  weatherlib/
    filters.py           # ondo/bcolor/jikan/weather_img（C# ViewUtility/Jma の移植）★実装済
    season.py            # is_summer/is_season（C# Jma の移植）★実装済
    stations.py          # 主要都市マスター（観測所コード⇔予報区域⇔PLACE）★実装済
    jma.py               # 気象庁クライアント（CSV/map JSON/予報 JSON/平年値）★実装済
    store.py             # 帳簿ストア（SQLite: ログ・地点マスタ）★実装済
    ncstore.py           # 観測値ストア（NetCDF-4: observations.nc）★実装済
  master/                # 変化しないマスターデータ（平年値等。初回取得後は再利用）
  store/observations.nc  # 観測値本体（NetCDF-4 単一ファイル。station×time/date 配列）
  store/weather.sqlite   # 帳簿（取込ログ・訂正ログ・地点マスタ）
  data/                  # 取得層の出力＝描画層の入力（DATA_CONTRACT 準拠）
  templates/             # Jinja2 テンプレート（Razor ビューの移植）★HighsMain 実装済
  public/                # 最終出力（配信物）
```

---

## 4. データソースの置き換え

> 取得場所の比較検討は [DATA_SOURCES.md](DATA_SOURCES.md) を参照（候補・評価・決定理由）。

| 旧ソース | 用途 | 新ソース | 検証 |
|----------|------|----------|------|
| `D:\Cache\Weather\Highs/Lows/*.json`（外部バッチ生成） | 今日の最高・最低気温、地点数、今季記録 | **気象庁 最新気象データ CSV**<br>`https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext00_rct.csv`（最高気温・全 914 地点）<br>`mntemsadext00_rct.csv`（最低気温） | ✅ 取得確認済 |
| GCP Datastore `forecastSummaries` | 都市の天気・最高気温予報 | **気象庁 予報 JSON API**<br>`https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json` | ✅ 取得確認済 |
| PostgreSQL `jma_dailynormal` `jma_dailynormal2` `jma_monthlynormal` `jma_yearlynormal` `jma_precipitation_yearlynormal` | 平年値（色分け・雨温図・月別気温・降水量ランキング）。**官署＋アメダス全地点** | **気象庁 平年値一括ダウンロード ZIP**（初回のみ取得しローカルマスター化）<br>官署: `normal_surface.zip`（日別・月別ほか）<br>アメダス: `normal_amedas_daily.zip`＋`normal_amedas_monthly.zip`（1362 地点）<br>地点マスタ: `amedas_station_index.zip`<br>（`https://www.data.jma.go.jp/stats/data/mdrr/normal/2020/data/` 配下） | ✅ 取得・中身確認済（要素コード 0500/0600/0700、東京で官署⇔アメダス一致） |
| PostgreSQL `jma_daily`（日別履歴・**全地点**） | 猛暑日・真夏日等の**日数**集計、年別・平均気温ランキング、東京の気温グラフ | **履歴ストア（SQLite）**: 最高/最低=確定値 CSV（7 日窓）、**日平均気温等=アメダス map JSON の毎正時 24 回集計（全 1286 地点・7 日分保持確認済み）**を毎日取り込んで蓄積。月次に etrn 日別値（daily_s1/daily_a1）で確定値へ置換。過去分は旧 PostgreSQL ダンプからインポート（入手できない場合は etrn からバックフィル or 蓄積開始日以降のみ表示） | Phase 2 |
| 外部 CDN `creativeweb.jp/gfs_img` | GFS 天気図画像 | **変更なし**（既に外部ホスト） | — |

### 4.1 気象庁 最新気象データ CSV の主なカラム（検証結果）

`mxtemsadext00_rct.csv`（CP932。1 行 = 1 観測地点、全国 914 地点）:

| 列 | 内容 | highsmain での用途 |
|----|------|--------------------|
| 1 | 観測所番号（アメダス番号。例 44132） | 予報 JSON の気温地点コードと突合 |
| 4 | 国際地点番号（例 47662 = 東京） | **旧サイトの 地点コード と同一** |
| 5–9 | 現在時刻（年/月/日/時/分） | `今日`（データ基準時刻） |
| 10–14 | 今日の最高気温(℃)・品質・起時 | `気温` `起時` |
| 15 | 平年差(℃) ※時間帯により空 | （空が多いため平年値マスターを使用） |
| 21–26 | 今年の最高気温（前日まで）と起日 | `最高気温` `最高気温日`（今季の記録） |
| 27–36 | 観測史上 1 位・当月 1 位と起日 | ランキング系ページ（Phase 2） |

集計での利用: `列10 >= 35` の行数 = **猛暑日地点数**、`>= 30` = **真夏日地点数**（旧 `manatubip.json` 相当）。

### 4.2 予報 JSON API の構造（検証結果）

`forecast/{office}.json`（office = 予報区域コードの上 3 桁 × 1000。例 東京 130010 → 130000）:

- `[0]` = 短期予報。`timeSeries[0]` = 区域ごとの天気・天気コード、`timeSeries[2]` = 地点ごとの気温
- 気温は **timeDefines の (日付, 時刻) で判別**する:
  - `今日 T09:00` → 今日の日中最高気温（予報）
  - `明日 T00:00` → 明日朝の最低気温、`明日 T09:00` → 明日の最高気温
- 天気は 区域コード（`StationToLocal` の値。C# からマスター移植）で引き、
  気温は CSV 列 1 のアメダス番号で引く。
- 旧 `CurrentPath`（"0"=今日予報 / "17"=明日予報）は reportDatetime の発表時刻から決定する。

### 4.3 マスターデータ

| ファイル | 内容 | 出所 | 更新頻度 |
|----------|------|------|----------|
| `master/stations.json` ★実装済 | 全 1286 地点マスタ（名前・カナ・座標・観測要素・官署番号・主要 57 都市の予報区域/PLACE） | アメダス地点表（bosai const）＋ mdrr CSV ＋ C# 移植定義。build_master.py が整合チェックつきで構築 | アメダス新設・廃止時に再実行 |
| `weatherlib/stations.py` | 主要 57 都市の定義（stations.json 構築の材料） | C# `Temperature.StationToLocal` + `Jma.MainCityName` から移植 | ほぼ不変 |
| `master/normals/{amedas}.json` | 日別・月別平年値（地点ごと） | 平年値一括 ZIP 4 本（**初回のみ**。master/raw/ に保存） | 平年値改訂時（次回 2031 年）のみ |

---

## 5. 日次データフロー

```
cron（毎日 数回。例: 6,10,14,18,22 時）
  │
  ├─ 1. fetch_data.py
  │     ├─ 最高気温 CSV・最低気温 CSV（最新値 `..00_rct.csv`）を取得（2 リクエスト）
  │     ├─ [Phase 2] 各日 24 時の確定値 CSV（`..{MMDD}.csv`）を**過去 7 日分**取得し
  │     │     履歴ストア(SQLite)に upsert（訂正が入るため毎回 7 日窓で再取得。
  │     │     蓄積値と異なればログに記録）→ 日数・ランキングを再集計
  │     ├─ 主要都市の予報 JSON を取得（office 単位で重複排除 ≒ 50 リクエスト、0.2 秒間隔）
  │     ├─ 平年値マスターを読む（初回のみ一括 ZIP から構築）
  │     └─ data/highsmain.json ほかを出力（DATA_CONTRACT 準拠）
  │          ※取得失敗時は既存の data/*.json を残す（前回値で生成続行）
  │
  ├─ 2. generate.py
  │     └─ data/*.json → public/*.html（アセットコピー含む）
  │
  └─ 3. デプロイ（rsync / オブジェクトストレージ同期 等）
```

- 全リクエストに User-Agent 明示・タイムアウト・リトライ（1 回）・アクセス間隔 ≥0.2 秒。
- 気象庁の更新タイミング（気温 CSV は毎時 50 分頃に毎時 00 分の値へ更新、予報は 5 時/11 時/17 時発表、
  確定値 CSV は 5・13・19・翌 1 時頃）に対し、cron は 1 日 5 回程度で十分
  （旧サイトも「5 時発表の予報を表示」と明記していた）。実行時刻は「毎時 50 分の反映後」＝**毎時 55 分**級に
  合わせると鮮度が最大になる（例: 5:55, 10:55, 13:55, 17:55, 21:55）。

---

## 6. ページ移行計画（旧 116 ルート → 段階移行）

### Phase 1 — 今日の気温系（CSV + 予報 + 平年値で完結）★パイロット実装中

| 旧ルート | テンプレート | data/ | 状態 |
|----------|--------------|-------|------|
| `/Temperature/HighsMain` | temperature/highsmain.html | highsmain.json | ★HTML 実装済・取得層実装中 |
| `/Temperature/LowsMain` | 同型（最低気温版） | lowsmain.json | 未 |
| `/Temperature/HighsList` `/LowsList`（各地・都道府県別） | 同型（一覧版） | highslist.json 等 | 未 |
| `/Temperature/TodayHighsDec/Asc` `/TodayLowsDec/Asc`（順位） | 同型（ソート版） | CSV 全 914 地点から生成 | 未 |
| `/`（Home） | home.html | home.json | 未（東京グラフは Phase 2） |

### Phase 2 — 日数・ランキング系（履歴ストアが必要）

| 旧ルート | 必要データ |
|----------|-----------|
| `/Summer/Ranking` `/SummerDayList` `/Hottest` `/HottestList`（年別あり） | 猛暑日・真夏日日数、年別最高気温 → SQLite に日次蓄積＋過去分バックフィル |
| `/Winter/*`（寒さ系） | 同上（冬日・真冬日） |
| `/Temperature/SummerMonth` `/WinterMonth`（日毎の地点数） | 地点数の日次記録（CSV 集計値を毎日保存） |
| Home の東京 30 日グラフ | 履歴ストアの日別値 |

### Phase 3 — 平年値・統計系（平年値マスター拡充）

| 旧ルート | 必要データ |
|----------|-----------|
| `/Monthly/*`（月別気温・平年値ランキング） | 月別平年値マスター＋月別履歴 |
| `/Climate/*` `/Stations/JP/{place}`（雨温図・地点詳細） | 月別平年値・雨量平年値（地点数が多い: 雨温図対象 150 地点前後） |
| `/Precipitation/*`（降水量ランキング） | 年間降水量平年値マスター |
| `/Stations/Clothes`（服装指数） | 地点予報（vpfd50 相当は予報 JSON で代替可） |

### Phase 4 — 仕上げ

- `/Gfs/*`（天気図）: 画像は外部 CDN のまま。ページだけ静的化（データ不要）。
- 旧 URL 互換: `WeatherController` 等のリダイレクト 15 本 → ホスティングのリダイレクト設定
  （Netlify `_redirects` / nginx / Cloudflare Rules 等）に落とす。
- モバイル: 旧 UA 判定 2 レイアウトはやめ、レスポンシブ 1 本に統一する。
- 検索(Google CSE)・AdSense・Analytics タグの復元判断。
- 旧サーバー停止。

---

## 7. 履歴ストア設計（Phase 2。参考）

SQLite `store/weather.sqlite`:

```sql
-- 日別観測値（CSV 由来。毎日 upsert）
CREATE TABLE daily (
  station   INTEGER NOT NULL,   -- 国際地点番号 (47662)
  date      TEXT    NOT NULL,   -- 'YYYY-MM-DD'
  tmax      INTEGER,            -- 最高気温 ×10（-999=欠測）
  tmax_time TEXT,
  tmin      INTEGER,
  tmin_time TEXT,
  PRIMARY KEY (station, date)
);
-- 日別の全国地点数（真夏日等。毎日 1 行）
CREATE TABLE daily_counts (
  date TEXT PRIMARY KEY,
  moushobi INTEGER, manatsubi INTEGER, natsubi INTEGER,   -- 猛暑日/真夏日/夏日 地点数
  nettaiya INTEGER, fuyubi INTEGER, mafuyubi INTEGER      -- 熱帯夜/冬日/真冬日 地点数
);
```

- **投入**: 各日 24 時の確定値 CSV（`mxtemsadext{MMDD}.csv` / `mntemsadext{MMDD}.csv`）を
  毎回過去 7 日分取得して upsert。気象庁側で 7 日間は訂正が入るため（毎日 4 回更新）、
  この 7 日窓の再取得で訂正へ自動追従する。蓄積済みの値と異なる場合は訂正としてログ出力。
- 品質フラグ（8=確定、5/4=準正常等）も保存し、集計時に利用できるようにする。
- 日数集計（例: 東京の今年の猛暑日日数）= `SELECT COUNT(*) FROM daily WHERE station=47662 AND date>='2026-01-01' AND tmax>=350`。
- 蓄積開始日より前のバックフィルは、旧 PostgreSQL `jma_daily` のダンプからの一括インポートが最速・最確実
  （入手できない場合は etrn 日別値ページから主要都市分のみ取得。全国地点数の過去分は復元不可）。

---

## 8. 非機能・運用

| 項目 | 方針 |
|------|------|
| 失敗時 | fetch 失敗 → 該当 data/*.json を更新せず前回値で generate（サイトは常に表示可能） |
| 原子性 | JSON/HTML は一時ファイルに書いて rename（生成途中の配信を防ぐ） |
| ログ | fetch/generate とも標準出力へ。cron 側でファイルに追記 |
| レート制限 | 気象庁へのアクセスは UA 明示・間隔 0.2 秒以上・リトライ 1 回 |
| タイムゾーン | すべて JST（`Asia/Tokyo`）で処理。サーバーの TZ に依存しない |
| 依存 | Python 3.10+、jinja2 のみ（HTTP は標準ライブラリ urllib。堅牢化したければ requests へ差し替え可） |
| 平年値改訂 | 2031 年予定の平年値更新時は master/ を削除して再取得 |

---

## 9. 決定事項・未決事項

**決定済み**
- 取得層は気象庁公開データ直取得（Datastore・外部バッチを廃止）
- HTML は Jinja2 事前生成（クライアント JS 描画にしない。既存の見た目を踏襲）
- モバイルはレスポンシブ 1 本化

**未決（実装を進めながら判断）**
- 履歴のバックフィル方法: 気象庁ページからのスクレイピング vs 既存 PostgreSQL ダンプのインポート
  （**既存 DB のダンプが入手できるなら後者を強く推奨**。7 章参照）
- 配信先ホスティング（rsync 先）: 未定
- AdSense / Analytics タグを新サイトへ載せるか: 未定

## 世界天気タイル(2026-07-07 計画、ユーザー決定「タイルは weather 側で必要なので作る」)

ECMWF オープンデータ(IFS/AIFS、0.25°格子、CC BY 4.0)から世界の天気の**ラスタータイル**を生成し、
weather.time-j.net の新しい世界天気ページと、time-j.net の世界地図(/WorldTime/Map)のレイヤーとして配信する。

### 方式(案)

- **要素(v1)**: 2m気温。v2 以降で 降水量・海面更正気圧(等圧線)・風。
- **タイル形式**: XYZ 256px PNG、z0〜4(世界表示用。z4=16×8タイル)。全ズーム合計 ≒ 340タイル/要素/時刻。
- **投影**: Web Mercator(標準の slippy 形式。将来 Leaflet 等でも使える)。/WorldTime/Map の正距円筒 SVG に重ねる場合は座標変換が必要な点に注意(Map 側を Mercator に揃える改修も選択肢)。
- **時刻**: 最新解析+予報 24/48/72h 程度から開始(タイル数を抑える)。
- **パイプライン**: fetch_tiles.py(ECMWF open data の .index で必要フィールドだけ HTTP Range 取得)
  → GRIB デコード → 格子を色分け描画 → タイル分割 PNG → public/tiles/{element}/{step}/{z}/{x}/{y}.png
- **依存**: eccodes(GRIB デコード)は conda-forge が安定(気象系は conda-forge の運用ルールに合致)。
  既存 .venv とは分離し、タイル生成専用の conda 環境を用意する。
- **配信**: Cloudflare Pages。タイルは毎回総入替になるため、1要素×4時刻×340タイル ≒ 1,400ファイル/回の
  デプロイ増となる点を確認(Pages 上限 20,000 に対しては問題なし)。
- **出典表記**: 「Data: ECMWF open data (CC BY 4.0)」をページ側に明記。

### 未決(実装開始時に確認)

- IFS か AIFS か(AIFS の方が新しいが要素が限られる)
- 色スケール(気温の配色は /WorldTime/Map の点表示と統一するか)
- 更新頻度(1日2回=00/12Z で十分か)
