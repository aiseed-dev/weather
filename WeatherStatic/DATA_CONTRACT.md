# データ契約（v2.1）— 取得層と描画層の境界のデータ形式

改訂: 2026-07-05
v1（ページ単位 JSON）→ v2（正規化）→ **v2.1（形式を「データの寿命と形」で使い分け）**

## 0. 設計方針

**形式は「データの寿命」で決める。年数で増え続けるものをファイル群に逃がさない。**

| 寿命 | 置き場所 | 形式 | 理由 |
|------|----------|------|------|
| **増え続ける**（履歴・日別値・年別集計） | `store/weather.sqlite` | **SQLite 一本** | 何年分でもテーブルが伸びるだけ。インデックスで速度維持、単一ファイルで管理が楽。ファイル分割（年別 CSV 等）は年数とともに管理不能になる |
| **毎回作り直す**（現在値スナップショット） | `data/` | **CSV（表）/ JSON（入れ子）** | 上書きされるだけで増えない。目視・diff できる |
| **不変**（マスター） | `master/` | **JSON** | 平年値改訂（次回 2031 年）まで変更なし |

### 層の責務

```
fetch_data.py   : ネットワーク → data/（現在値スナップショット。毎回上書き）
accumulate.py   : ネットワーク → store/weather.sqlite（履歴の蓄積・集計テーブル維持）
build_master.py : ネットワーク → master/（初回のみ）
generate.py     : data/ + master/ + store/weather.sqlite（読み取り専用）→ public/*.html
                  ※ネットワークアクセスなし。store への書き込みなし
```

- v2 で定義した「履歴集計の JSON エクスポート（history_summary.json / rankings/*.json）」は**廃止**。
  年数で増えるデータの二重管理になるため、描画層が SQLite を直接（読み取り専用で）参照する。

## 1. 共通規約

| 項目 | 規約 |
|------|------|
| 文字コード | UTF-8（BOM なし）。キー・列名は英語スネークケース |
| 温度・降水量・日照などの観測値 | **×10 整数**（25.3℃→253、39.0mm→390）。欠測は null（JSON）/ 空欄（CSV）/ NULL（SQLite） |
| 品質フラグ | 気象庁の品質情報コードをそのまま整数で（8=確定、5=準正常、4=速報 等） |
| 地点キー | **code（地点番号）= 国際地点番号（官署）、無ければ etrn の 4 桁地点番号**。気象庁の統計体系の番号で安定、**旧サイト DB の 地点コード と同一体系**（全国一意を検証済み）。アメダス番号は**改番が多いため主キーにしない**（現在番号という属性）。observations.nc の行番号（row）は内部割当 |
| 日付・時刻 | `"YYYY-MM-DD"` / `"YYYY-MM-DDTHH:MM"` / 起時 `"HH:MM"`（すべて JST） |
| CSV | ヘッダ行あり・LF・欠測は空欄。メタ情報（基準時刻等）は行に混ぜず、対の `*_meta.json` に分離 |
| 表示整形 | 「7月2日」「25.3℃」等はデータに入れない（描画層のフィルタで整形） |
| 書き込み | 一時ファイル → rename（原子的）。取得失敗時は前回のファイルを残す |

## 2. master/ — 不変マスター（build_master.py・初回のみ）

### 2.1 `master/stations.json` — 地点マスタ ★実装済（build_master.py）

ソース: **アメダス地点表**（`jma.go.jp/bosai/amedas/const/amedastable.json`・全 1286 地点）
＋ mdrr CSV 由来の sqlite stations（国際地点番号・都道府県）
＋ C# 移植の主要都市定義（PLACE・予報区域）＋ 平年値 ZIP（has_normals）。

```jsonc
{
  "_meta": {"built_at": "...", "source": "...", "station_count": 1286},
  "index": {"amedas_to_code": {"44132": 47662, ...}},   // 現在のアメダス番号からの逆引き
  "stations": {
    "47662": {                           // ← キーは code（地点番号: 国際地点番号 or etrn 4桁）
      "amedas": "44132",                 // 現在のアメダス観測所番号（改番されうる属性）
      "row": 510,                        // observations.nc の行番号（内部割当・不変）
      "name": "東京", "kana": "トウキョウ", "en": "Tokyo", "pref": "東京都",
      "intl": 47662,                     // 国際地点番号（官署のみ。それ以外は null）
      "lat": 35.6917, "lon": 139.75, "alt": 25,
      "type": "A",                       // 官署=A/B、アメダス=C など（amedastable の種別）
      "elements": {"temp": true, "precip": true, "snow": true, "sun": true},
      "has_normals": true,               // 平年値 ZIP に日別平年値があるか（新設地点は false）
      "etrn": {"prec_no": 44, "block_no": "47662", "type": "s"},   // 統計ページ用
      // ---- 主要 57 都市のみ ----
      "main": true,
      "place": "Tokyo",                  // 旧 /Stations/JP/{PLACE} の URL 部品
      "area": "130010", "office": "130000",
      "pref_code": "13"
    }
  }
}
```

**主キーは code（地点番号）= 国際地点番号 or etrn 4 桁番号。番号は 4 体系を属性として持つ**:

| 体系 | 例（東京 / 府中） | 使う場所 | stations.json での持ち方 |
|------|------------------|----------|--------------------------|
| **code（地点番号）** | **47662 / 1133** | **主キー**。旧サイト DB の 地点コード と同一体系（旧 DB バックフィルはキー変換不要）。全国一意（4 桁側 2〜1678、官署側 47401〜47991、衝突なし） | キー |
| row（nc 行番号） | 510 / 508 | observations.nc の station 行（内部割当・不変） | `row` |
| アメダス観測所番号 | 44132 / 44116 | map JSON・amedastable・mdrr CSV 列 1・平年値アメダス ZIP | `amedas`（**改番されうる属性**。履歴は sqlite amedas_log） |
| 国際地点番号（官署のみ） | 47662 / なし | etrn 官署ページ・平年値官署 ZIP | `intl`（官署では code と同値） |
| etrn の prec_no/block_no | 44/47662 ・ 44/1133 | 過去の気象データ検索（daily_a1.php 等の URL） | `etrn: {prec_no, block_no, type}` |

- **官署に 4 桁の別番号は無い**: etrn の block_no は官署では国際地点番号そのもの（全 155 官署で
  一致を検証済み。type='s'）。独自 4 桁番号を持つのはアメダス単独点（type='a'、1131 地点）のみ。
- 南鳥島（intl 47991）・富士山（intl 47639）は mdrr CSV に現れないため、etrn から intl を補完。

- etrn の突合は**座標（度・分）一致**で行う（build_master.py。全 1286 地点突合済み。
  官署は etrn block_no == 国際地点番号 を独立検証として確認済み）。
- elems フラグは実測特定: [0]=気温 [1]=降水量 [5]=積雪 [6]=日照（[2,3]=風、[4,7]=気圧系・官署のみ）。
- 都道府県は mdrr CSV 由来（気温観測地点）。気温の無い 372 地点はエリア番号（先頭 2 桁）の
  多数決で補完（北海道のエリア 15・24 は複数振興局が混在するため近似になる）。
- 検証済み（2026-07-05）: 地点集合は map JSON・amedastable で完全一致（1286）。
  主要 57 都市すべて解決。気温フラグ 916 と mdrr CSV 914 の差は 南鳥島・富士山
  （ランキング対象外の特殊地点）。気温観測ありで平年値なし＝新設 5 地点。

**アメダス番号の改番への対応**（code 主キー化により系列は切れない）:
- sqlite `stations` に `first_seen` / `last_seen`（観測データに現れた期間）を記録（accumulate が毎回更新）。
- build_master.py の再実行時に前回との差分（新規・消滅・座標近接による**改番候補**）を報告。
- 改番が判明したら **`build_master.py --renumber 旧番号 新番号`** を実行:
  code・row はそのままアメダス属性だけ付け替え、履歴を `amedas_log` に記録、
  observations.nc の station_id も更新 → **観測系列は同じ code・同じ行に継続**（切断なし）。
- etrn 未掲載の新地点は code=NULL で蓄積を開始し、etrn 掲載後に build_master が code を解決して埋める
  （それまで stations.json・today.csv には現れない）。
- 誤って新 row が発番された後に改番と判明した場合の統合は手動対応（頻度が低い）。

### 2.1b `master/station_codes.json` — 地点番号の全一覧（現役＋廃止） ★実装済（fetch_station_codes.py）

ソース: etrn 地点選択ページ（`stats/etrn/select/prefecture.php?prec_no=NN`・全 61 府県）の
viewPoint(...) から、**廃止地点を含む**全地点番号を取得（過去データのバックフィル時に必要）。

```jsonc
{
  "_meta": {"fetched_at": "...", "entry_count": 1678},
  "entries": [
    {"prec_no": 88, "block_no": "1491", "type": "a", "name": "中甑", "kana": "ナカコシキ",
     "lat": 31.8333, "lon": 129.8583, "lat_dm": [31, 50.0], "lon_dm": [129, 51.5], "alt": 20.0,
     "flags": {"precip": 1, "wind": 1, "temp": 1, "sun": 2, "snow": 0, "humidity": 1},
     "active": true, "end": null}          // 廃止地点は active:false, end:"YYYY-MM-DD"
  ]
}
```

- 検証済み（2026-07-05）: 全 1678 地点（現役 1287=官署 156＋アメダス 1131、廃止 391）。
  現役の地点番号は全国一意（富士山 47639 は山梨・静岡へのクロス掲載で重複ではない）。
  **廃止を含めても番号の別地点への転用ゼロ** → code は歴史的にも安定。
  公式コード表（防災情報 XML の PointAmedas.xlsx）と**座標で全 1287 地点一致**。
- 突合用の公式コード表: https://xml.kishou.go.jp/tec_material.html の個別コード表 zip を
  `master/raw/jmaxml_code.zip` に置くと fetch_station_codes.py が自動で突合する。
- build_master.py はこのファイルがあれば etrn を再取得せずに使う（更新したい時だけ
  fetch_station_codes.py を実行）。

### 2.2 `master/normals/{amedas}.json` — 平年値（1991–2020）・地点ごとに 1 ファイル

ソース: 平年値一括 ZIP 4 本。**平年値は常に地点単位でしか参照しない**ため地点別に分割
（約 1362 ファイル・各 15〜20KB）。描画層は必要な地点の分だけ読む。

```jsonc
// master/normals/44132.json（東京）
{
  "amedas": "44132", "intl": 47662,
  "daily": {                    // 月番号 → 要素 → 日別配列（index 0 = 1 日）
    "7": {
      "tavg": [240, 242, 243, 244, 246],
      "tmax": [280, 282, 283, 285, 286],
      "tmin": [207, 208, 210, 211, 212],
      "precip": [39, 40, 41]
    }
  },
  "monthly": {                  // 12 か月（index 0 = 1 月）＋年間値
    "tavg": [54, 61, 94, 143, 188, 219, 257, 269, 233, 180, 125, 77],
    "tmax": [], "tmin": [], "precip": [], "sun": [],
    "year": {"tavg": 158, "tmax": null, "tmin": null, "precip": null, "sun": null}
  }
}
```

- 参照例: 7 月 5 日の最高気温平年値 = `daily["7"]["tmax"][4]`
- 要素コード対応: 0500→tavg, 0600→tmax, 0700→tmin（官署・アメダス共通。検証済み）
- 官署とアメダスの重複地点（東京 47662/44132）はアメダス番号 1 ファイルに統一、値は官署優先
- ファイル名は ZIP 提供時点のアメダス番号（JMA の配布キー）。参照は stations.json の
  `amedas` 属性経由で行い、改番があった場合は amedas_log の旧番号でフォールバックする

## 3. data/ — 現在値スナップショット（cron 毎に上書き。増えない）

### 3.1 `data/today.csv` ＋ `data/today_meta.json` ★fetch_data.py

ソース: mdrr 最新値 CSV（最高・最低）。1 行 = 1 地点（約 914 行）。

```csv
code,amedas,tmax,tmax_at,tmax_q,tmin,tmin_at,tmin_q,year_tmax,year_tmax_date,year_tmin,year_tmin_date,record_tmax,record_tmax_date,month_tmax,month_tmax_date
47662,44132,259,09:58,4,220,05:12,4,318,2026-06-01,-12,2026-01-25,395,2004-07-20,395,2004-07-20
```

```jsonc
// data/today_meta.json — 表に入らないメタ情報
{
  "source_time": "2026-07-05T10:00",     // CSV の現在時刻（データ基準時刻）
  "counts": {                             // 全国の地点数集計
    "moushobi": 0, "manatsubi": 23, "natsubi": 210,   // 猛暑日/真夏日/夏日 (tmax>=350/300/250)
    "mafuyubi": 0, "fuyubi": 0, "nettaiya": 41        // 真冬日/冬日/熱帯夜 (tmax<0 / tmin<0 / tmin>=250)
  }
}
```

### 3.2 `data/forecast.json` ★fetch_data.py

ソース: bosai 予報 JSON（office 単位で取得し、主要都市の地点に解決済み）。小さい入れ子なので JSON。

```jsonc
{
  "reported": "2026-07-05T05:00",
  "target_date": "2026-07-05",
  "target_label": "today",               // 参考情報（取得時点の判定）
  "stations": {                          // キーは code（地点番号）→ **実日付**（再取得省略時も安全）
    "47662": {
      "2026-07-05": {"weather": "くもり 時々 雨", "wcode": "203", "tmax": 27, "tmin": 21},
      "2026-07-06": {"weather": "くもり", "wcode": "200", "tmax": 25, "tmin": 20}
    }
  }
}

- 予報の発表は 1 日 3 回（5・11・17 時）のみのため、既存ファイルが最新発表分なら再取得しない
  （負荷抑制）。描画層は生成時点の日付でエントリを引く（today に tmax が無ければ tomorrow に切替）。
```

### 3.3 `data/current.json` — 現在の天気・気温（主要都市） ★fetch_data.py

「現在」の 2 ソース併用:
- **気温 = アメダス最新 10 分値**（実測。`bosai/amedas/data/latest_time.txt` → `map/{ts}.json`）
- **天気 = 推計気象分布（suikei）**: 毎時更新のタイル PNG（512px・z=10）を
  都市の緯度経度 → タイル座標・ピクセル位置でサンプリングし、凡例色 → カテゴリに変換
  （weatherlib/suikei.py。色は凡例 SVG 由来の確定値。**z=8**（1px≈0.6km、1km メッシュの精度をそのまま維持）で 57 都市 22 タイル）。

```jsonc
{
  "amedas_time": "2026-07-05T20:00",   // 気温の観測時刻（10 分毎）
  "wthr_time": "2026-07-05T20:00",     // 推計天気の対象時刻（毎時）
  "stations": {                        // キーは code
    "47662": {"temp": 234, "wthr": "くもり", "wcode": "200"}
    // wthr は 晴れ/くもり/雨/雨または雪/雪、データ外は null
  }
}
```

- 取得失敗時は current.json を更新せず前回値を維持（Home は current 無しでも生成可能）。

### 3.4 服装投票（vote.gif ビーコン → アクセスログ集計） ★実装済

静的サイトのままバックエンド無しで投票を受ける仕組み:

```
ブラウザ ─ GET /vote.gif?d=2026-07-06&c=47662&v=1 ─▶ 配信サーバー（ログに記録されるだけ）
                                                        │
ローカル Python: aggregate_votes.py access.log ◀────────┘（1 日 1 回程度）
    → store/weather.sqlite の votes_raw に蓄積（IP は SHA-1 化、(IP,日,地点) で重複排除・冪等）
    → generate.py が「昨日の投票結果（合ってた %）」を Home に表示
```

- クライアント側は localStorage で同日同地点の再投票を抑止（表示は「✓ 投票済」）。
- vote.gif（1x1 透明 GIF）は generate.py が public/ に配置。

### 3.5 クライアント側ライブ更新（Home） ★実装済

気象庁の bosai 系エンドポイントは **CORS 全開放（Access-Control-Allow-Origin: *）を確認済み**。
Home はビルド時の現在値を初期表示しつつ、**ブラウザが気象庁から直接**最新値を取得して
10 分毎に表を更新する（当方サーバー・cron 頻度に依存しない鮮度。JS はプレーンな vanilla）:

- 気温: `latest_time.txt` → `amedas/data/map/{ts}.json`（表示 10 都市分を 1 リクエストで）
- 天気: `suikeikishou/targetTimes.json` → wthr タイル（z=8）を `crossorigin=anonymous` で
  canvas に描き、都市ピクセルの色 → カテゴリ判定（凡例色はサーバー実装と共通）
- 服装の目安・傘マークもクライアント側で再計算。取得失敗時はビルド時の値のまま（グレースフル）。

## 4. store/ — 履歴（accumulate.py が書き、generate.py は読み取り専用）

**観測値本体は NetCDF-4 単一ファイル、帳簿は SQLite**（[STORAGE_FORMATS.md](STORAGE_FORMATS.md) 案 B・採用済み）。

### 4.1 `store/observations.nc` — 観測値（NetCDF-4。何年でもこの 1 ファイル）

```
次元:  station(unlimited) × time(unlimited: 毎正時) × date(unlimited: 日)
       時間軸は線形インデックス = 配列添字
         time index = (JST 時刻 − 2020-01-01T00:00) の時間数
         date index = (日付 − 1870-01-01) の日数     ※過去バックフィル余地
変数（×10 の int16、欠測 -32768。品質・回数は int8、欠測 -1。zlib5+shuffle）:
  時別:  temp / precip1h / sun1h                 [station][time]   ← map JSON 毎正時
  日別:  tmax, tmax_minutes(0時からの分), tmax_q  [station][date]   ← 確定値 CSV（official）
         tmin, tmin_minutes, tmin_q
         tavg, tavg_count, tavg_q                ← 時別から集計（毎正時 24 回平均）/ q はバックフィル由来
         precip, precip_q, precip_none, sun      ← 時別から集計 / q・降水無はバックフィル由来
補助:  station_id[station]（アメダス番号）、time/date（座標変数）
```

- station 行番号 = **row**（SQLite `stations.row` が正。内部割当・不変。station 次元も unlimited）。
  契約上の主キー code → row の対応は stations.json（または sqlite）で引く。
  アメダス番号が改番されても code・row は不変なので、**観測系列は同じ行に継続**する。
- 訂正（7 日窓）は in-place 上書き＋ correction_log へ記録。
- 書き込みは「コピー → 更新 → rename」で原子的に（クラッシュ耐性）。
- 実測: 7 日分で 0.5MB ≒ **0.025GB/年**。10 年でも単一ファイル 250MB 程度。

描画層の参照例（numpy スライス）:

```python
ds = netCDF4.Dataset("store/observations.nc"); ds.set_auto_mask(False)
i = stations["47662"]["row"]                # code から nc 行番号を引く（stations.json）
jd = date_index(date(2026, 7, 4))
tokyo_31days = ds["tmax"][i, jd-30:jd+1]    # 東京の直近 31 日（Home グラフ）
tmax_all = ds["tmax"][:, jd]                # 7/4 の全地点（順位・地点数カウント）
year = ds["tmax"][i, date_index(date(2026,1,1)):jd+1]
moushobi_days = int(((year != FILL) & (year >= 350)).sum())   # 今年の猛暑日日数
```

### 4.2 `store/weather.sqlite` — 帳簿（小さいまま）

```sql
stations   (row PRIMARY KEY, code UNIQUE, amedas UNIQUE,  -- code=地点番号(契約上の主キー)
            intl, pref, name, first_seen, last_seen)       -- row=nc行番号(内部) amedas=現在番号
amedas_log (row, amedas, valid_from, valid_to)             -- アメダス番号の履歴
ingest_log       (kind, key, fetched_at)                             -- 取込済み記録
correction_log   (logged_at, amedas, date, field, old_value, new_value)
```

- 過去分バックフィル ★実装済（backfill_daily.py）: 旧 PostgreSQL の jma_daily
  （WeatherToolsCore が蓄積。最高/最低/平均気温・降水量、×10 整数＋気象庁品質注）を
  pg_dump（COPY 形式）または CSV から取り込む。**地点コード＝code 同一体系のため無変換**。
  既存セルは上書きしない（現行蓄積を優先）。未知の廃止地点は行を新規割当し
  名前を station_codes.json から補完。date 軸は 1870 年起点なので数十年前でもそのまま入る。

## 5. ページとデータの対応（描画層の組み立て）

| ページ | 使うデータ |
|--------|-----------|
| Home | today + forecast + store(東京 31 日, day_counts) + normals |
| Temperature/HighsMain・LowsMain | today(主要 57 都市) + forecast + normals + store(今年の日数) |
| Temperature/HighsList・LowsList | today(全地点・都道府県グループ) + normals |
| Temperature/TodayHighsDec/Asc 等 | today(全地点ソート) |
| Temperature/SummerMonth・WinterMonth | store(day_counts) |
| Summer/Winter 系ランキング | store(daily 集計) |
| Monthly/Climate/Stations（Phase 3） | normals(monthly) + store(月別集計) |
| Precipitation（Phase 3） | normals(precip) + store |

季節・「今日/昨日」文言は today_meta.source_time と生成時刻から描画層が判定。
平年差の色は `bcolor(today.tmax - normals.daily[月][tmax][日-1])`。

## 6. v2 からの変更点

| v2 | v2.1 |
|----|------|
| data/today.json（JSON） | **data/today.csv + today_meta.json**（表は CSV） |
| data/history_summary.json・rankings/*.json（エクスポート） | **廃止** → 描画層が store/weather.sqlite を読み取り専用で直接参照 |
| 「描画層は data/ と master/ のみ読む」 | 「＋ store/weather.sqlite（読み取り専用）」に変更 |
| master/normals.sqlite 案 | 廃止済み → master/normals/{amedas}.json（地点別 JSON） |

## world — worldtime-web(time-j.net)向けの世界の天気配信

fetch_world.py が生成(2026-07-07 追加)。都市マスターは `master/world_cities.json`
(**worldtime-web 側の tools/export_world_cities.py が生成**。都市の一次管理は worldtime)。
出力は `data/world/` → `public/data/world/` に同期し、`public/_headers` の
`/data/world/*` に `Access-Control-Allow-Origin: *` を保証する(www.time-j.net からの fetch 用)。

### data/world/forecast/{place}.json(112都市、ソース中立スキーマ)

```json
{
  "place": "Europe/London", "name": "ロンドン",
  "updated": "…",                  // ソースの発表時刻(UTC)
  "fetched": "…", "source": "MET Norway (CC BY 4.0)",
  "hourly": [ {"t": "…Z", "temp": 20.9, "sym": "cloudy", "pre": 0.5,
               "wind": 2.9, "wdir": 325, "rh": 90}, … ],   // 直近48時間
  "daily":  [ {"date": "2026-07-08", "tmin": 18.8, "tmax": 30.5,
               "pre": 0.0, "sym": "clearsky_day"}, … ]     // 現地日付で8日分
}
```

- daily は都市のタイムゾーンで集計(tmin/tmax=instant 気温の min/max、pre=next_1h 優先の降水量合計、sym=現地正午に最も近い時点の symbol_code)。
- ソース差し替え(Open-Meteo 等)をしても worldtime 側が壊れないよう、met.no 固有の構造は持ち込まない(sym は met.no symbol_code 語彙)。

### data/world/metar/{icao}.json(385局)

```json
{"icao": "RJTT", "time": "…Z", "temp": 23, "dewp": 19, "wdir": 20, "wspd_kt": 10,
 "wgst_kt": null, "visib": "6+", "wx": "-RA", "clouds": [{"cover": "BKN", "base": 3000}],
 "flt_cat": "VFR", "raw": "METAR RJTT …", "source": "aviationweather.gov", "fetched": "…"}
```

- 通報の無い局(3割弱)はファイルを更新しない(前回値を残す)。初回から通報が無い局はファイル自体が無い → 404 は worldtime 側で許容。

### data/world/index.json

`{"forecast": [place…], "forecast_updated": "…", "metar": [icao…], "metar_updated": "…"}`

### 運用

- cron: 既存の日次実行に `fetch_world.py` を追加(全量で約2分30秒、met.no 112リクエスト+aviationweather 4リクエスト)。METAR だけ高頻度にする場合は `--metar-only`。
- `generate.py --clean` 後は `fetch_world.py --sync-only` で public/ に再同期する。

### data/world/map.json(地図描画用・全都市1ファイル)

`{"updated": "…Z", "cities": [{"p": "Asia/Tokyo", "t": 23, "s": "cloudy"}, …]}`
(t=METAR 実測気温、s=直近の予報 symbol_code。どちらかがある都市のみ収録。
fetch_world.py が取得済みファイルから組み立てる。利用者: time-j.net/WorldTime/Map)
