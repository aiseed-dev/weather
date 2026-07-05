# 気象データの保存形式 調査

作成: 2026-07-05。本プロジェクトの履歴ストア（アメダス地点×時刻の観測値、年数無制限で蓄積）に
適した保存形式の調査。実データ（hourly 216,048 行 = 7 日分）でのベンチマーク実測つき。

## 1. 気象分野の標準形式（WMO・科学系）

| 形式 | 用途 | 本件への適合 |
|------|------|--------------|
| **GRIB2** | WMO 標準。**格子点データ**（数値予報モデル出力: GFS・MSM 等）。気象庁の GPV 配信もこれ | ✗ 格子データ専用。地点観測の時系列には使わない |
| **BUFR** | WMO 標準。**観測データの国際交換**（SYNOP 通報等はBUFR化済み） | ✗ 伝送・交換用。テーブル駆動で eccodes 必須、解析・蓄積用途には不向き |
| **NetCDF-4 + CF 規約** | 大気科学のデファクト標準。多次元配列（station × time × 要素）。CF の timeSeries featureType が地点時系列用 | △ 「正しい」科学標準だが、netCDF4 ライブラリ依存・追記が不得手（期間ごとにファイルを切るのが常道）・SQL 的な集計（しきい値カウント等）には配列処理が必要 |
| **Zarr** | クラウドネイティブなチャンク配列。Met Office が NWP 出力 200TB/日の保存に採用、NetCDF 側も NCZarr で追随 | △ 大規模格子・クラウド並列読み向け。ローカル単機の地点時系列には過剰 |
| **HDF5** | 汎用科学コンテナ（NetCDF-4 の下層） | △ NetCDF に同じ |

→ **WMO 系（GRIB/BUFR）は役割が違う**。NetCDF/Zarr は「多次元配列・大規模・共有」が
主戦場で、本件（表形式・単機・SQL 集計中心）では利点よりライブラリ依存と運用の複雑さが勝る。

## 2. データエンジニアリング系

| 形式 | 特徴 | 本件への適合 |
|------|------|--------------|
| **SQLite** | 標準ライブラリのみ。トランザクション・**upsert・部分更新が得意** | ◎ 訂正反映（7 日窓）に最適。ただし行志向でサイズが大きい（実測 1.1GB/年） |
| **Parquet**（zstd 圧縮） | 列指向・不変ファイル。DuckDB/pandas から直接 SQL/読込可 | ◎ **圧縮が圧倒的**（実測 13MB/年 ≒ SQLite の 1/100）。ただし**追記・更新不可**（ファイル置き換えのみ）→ 確定済みデータのアーカイブ向け |
| **DuckDB** | 単一ファイルの分析 DB。Parquet を直接 SQL できる | ○ 集計が速い。ただし書き込みの並行性・成熟度は SQLite に劣る。「Parquet を読む道具」として使うのが良い |
| TimescaleDB / InfluxDB | 時系列 DB サーバー | ✗ サーバー常駐が必要。「静的サイト＋ローカル定期実行」の方針に反する |
| CSV (+gzip) | 汎用・目視可能 | ○ スナップショット・受け渡し用（採用済み）。蓄積本体には不向き（更新・索引なし） |

## 3. 実測ベンチマーク（実データ: hourly 216,048 行・7 日分）

| 形式 | 実測サイズ | 年間換算（11.3M 行） | しきい値カウント |
|------|-----------:|--------------------:|------------------:|
| SQLite（現行スキーマ） | 21.3 MB | **1.11 GB/年** | 29 ms |
| CSV 素 | 6.7 MB | 0.35 GB/年 | — |
| CSV + gzip | 1.1 MB | 0.06 GB/年 | —（要展開） |
| **Parquet（zstd）** | **0.3 MB** | **0.013 GB/年** | 3 ms |
| DuckDB ネイティブ | 1.6 MB | 0.08 GB/年 | 2 ms |

- Parquet が極端に小さいのは列指向＋zstd が「単調な時刻列・相関の強い観測値」に効くため。
- SQLite が大きいのは行志向＋TEXT キー（ts/amedas）＋B-tree のオーバーヘッド。
  スキーマ最適化（epoch 整数化・WITHOUT ROWID）でも 1/3 程度までで、Parquet には遠く及ばない。
- daily テーブルは 0.47M 行/年 ≒ **50MB/年程度**で、20 年貯めても 1GB — 問題にならない。

## 3.5 NetCDF-4 深掘り（実データ検証・2026-07-05 追記）

初回調査で「△（追記が不得手）」としたが、実データで検証した結果**大幅に評価を修正**する。

### 構造と機能

- 実体は **HDF5 コンテナ**。変数 = N 次元配列（本件: `temp[station][time]` の int16）＋
  チャンク単位の圧縮（zlib / **zstd**〈netCDF-C 4.9+〉）＋ shuffle フィルタ。
- **CF 規約**（Climate and Forecast Conventions）でメタデータを標準化。地点時系列は
  `featureType = "timeSeries"`（Discrete Sampling Geometry）として表現するのが正式。
  単位・欠測値・地点 ID がファイル内に自己記述され、xarray / Panoply / ncdump 等でそのまま読める。
- **unlimited 次元は複数持てる**（NetCDF-4/HDF5）→ time と station の両方を可変にできる。

### 実データ検証の結果（hourly 216,048 行 → 1286 地点×168 正時の int16 配列×3 要素）

| 検証項目 | 結果 |
|----------|------|
| サイズ（zlib5+shuffle） | **0.48MB = 0.025 GB/年**（SQLite の 1/44、Parquet の約 2 倍） |
| サイズ（zstd5+shuffle） | 0.50MB（ほぼ同等） |
| しきい値カウント（全読み+numpy） | **4ms**（SQLite 29ms、DuckDB 2ms と同級） |
| 1 地点の時系列読み | 0.4ms（チャンク単位の部分読み） |
| **in-place 更新（訂正）** | **成功** — 7 日窓の訂正 upsert に使える |
| **time 方向の追記** | **成功**（unlimited 次元） |
| **station 方向の追加**（アメダス新設対応） | **成功**（複数 unlimited 次元） |

→ 「追記・更新不可」という当初評価は誤り。**Parquet と違い、単一ファイルのまま
何年でも伸ばせて、訂正も直接書ける。**

### 残る注意点（Parquet/SQLite との本質的な差）

| 項目 | 内容 |
|------|------|
| クラッシュ耐性 | HDF5 にトランザクションは無い（SQLite の WAL のような保護なし）。書き込み中の中断でファイル破損のリスク → 「コピー→更新→rename」運用か、直近 7 日は元データから再取得可能なことを保険にする |
| 帳簿類が入らない | ingest_log / correction_log のような可変長・関係データは配列モデルに合わない → 小さな SQLite か JSON を併用 |
| 文字列フィールド | 起時 "HH:MM" は分整数（int16）に、品質フラグは int8 変数にする等の設計が必要 |
| SQL が無い | 集計は numpy/xarray（ベクトル演算。実測は十分速い） |
| 依存 | `netCDF4` ホイール（HDF5 同梱）。DuckDB より重いが pip 1 発 |

## 4. 結論 — 2 案（NetCDF-4 検証後の改訂）

> **【決定】案 B を採用（2026-07-05）。** 理由: 観測所・日付を配列スライスで自由に切り取れる、
> 蓄積本体が永久に単一ファイル、気象データの標準形式。実装・移行・検証済み
> （東京 4 日分が etrn 公式値と全一致、observations.nc 0.5MB / 帳簿 sqlite 0.11MB）。

### 案 A: ホット = SQLite / コールド = Parquet（初回結論）

```
store/weather.sqlite        # 直近＋帳簿（訂正 upsert・ログ）。daily は全期間
store/archive/hourly_{YYYY}.parquet   # 年次で確定分を書き出し、SQLite から削除
```

- 利点: 訂正は SQLite の得意技のまま。過去年横断は DuckDB の SQL。
- 欠点: 二層の移送バッチが必要。年ごとにファイルが増える（管理対象は単純だが複数）。

### 案 B: 観測値 = NetCDF-4 単一ファイル / 帳簿 = SQLite（深掘り後の新案）

```
store/
  observations.nc           # hourly/daily の全履歴を 1 ファイルで持ち続ける
                            #   temp[station][time] 等の int16 配列（zstd+shuffle）
                            #   訂正は in-place、時間・地点とも unlimited で追記
                            #   10 年でも ~250MB・気象データの標準形式（CF 規約）
  weather.sqlite            # ingest_log / correction_log / stations の帳簿のみ（小さい）
```

- 利点: **蓄積本体が永久に 1 ファイル**（年数が増えても管理対象が増えない）。
  移送バッチ不要（ホット/コールドの区別が消える）。気象標準形式で自己記述・他ツール互換。
- 欠点: クラッシュ耐性は運用でカバー（コピー→更新→rename。10 年分でも 250MB なので毎回コピーしても軽い）。
  集計は SQL でなく numpy（実測は高速）。

**判断基準**: SQL の操作性を最優先なら A、
「単一ファイルで何年でも・気象標準形式」を優先なら B。
どちらも実測済みで技術リスクは低い。データ量・速度はともに問題なし。

### 共通の決定

- 生の取得ファイル（mdrr CSV・map JSON）はアーカイブしない（ストアから再現可能）。
- daily（確定値）はどちらの案でも全期間保持が正。hourly は日集計の材料なので、
  etrn 月次確定後は間引き/削除してもよい。

## 参考

- [Best Practices for Storing NetCDF and Zarr Datasets（21st Century Weather Wiki）](https://21centuryweather.github.io/21st-Century-Weather-Software-Wiki/python/netcdf_zarr_storage_best_practices.html)
- [Cloud Native Geospatial Formats: GeoParquet, Zarr, COG, PMTiles](https://forrest.nyc/cloud-native-geospatial-formats-geoparquet-zarr-cog-and-pmtiles-explained/)
- [Benchmarking Zarr and Parquet Data Retrieval (Element 84)](https://element84.com/software-engineering/benchmarking-zarr-and-parquet-data-retrieval-using-the-national-water-model-nwm-in-a-cloud-native-environment/)
- [Using Cloud Computing to Analyze Model Output Archived in Zarr Format (NOAA)](https://repository.library.noaa.gov/view/noaa/60380/noaa_60380_DS1.pdf)
