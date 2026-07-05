# データ取得設計書

作成: 2026-07-06。本書の数値はすべて日本国内の開発機からの**実測**に基づく。

## 結論（どこから・どうやって取るか）

| データ | 取得場所 | 実行場所 | 根拠 |
|--------|----------|----------|------|
| ECMWF Open Data（IFS/AIFS 予報） | **GCS ミラー** `storage.googleapis.com/ecmwf-open-data` | ローカル（アプリ/cron） | 日本から 10MB=1.5 秒・50MB 持続 14MB/s。AWS eu-central-1 は 503 Slow Down を実測 |
| ERA5 気候値 | WeatherBench2 計算済み気候値（GCS）/ ARCO-ERA5（GCS） | **GCP（Colab）のみ**。成果物パックだけ持ち帰る | 自前計算は 1 変数 ≈45GB 読み。データの隣で計算し、100MB 級のパックを配る |
| JMA（現況・レーダー・アメダス） | bosai 系エンドポイント | ローカル/クライアント | 軽量。CORS 全開放（`*`）確認済み |

## 1. ECMWF Open Data の取得

### ミラー選択（実測 2026-07-06）

| ミラー | 日本からの実測 |
|--------|----------------|
| AWS `ecmwf-forecasts`（eu-central-1） | **HTTP 503 Slow Down**（連続アクセスで容易に発生） |
| **GCS `ecmwf-open-data`** ★推奨 | HTTP 206 正常。10MB=1.5 秒、50MB 持続 **14MB/s** |
| ECMWF 直（data.ecmwf.int） | フォールバックのみ（接続数制限 500） |

GCS は `storage.googleapis.com` が Google のグローバル網（東京入口）でフロント
されるため、アジアから最速。`ecmwf-data-access` スキルの「GCP mirror — often
best for Asia-Pacific」を既定に格上げする根拠となる実測値。

- config.toml のミラー選択肢には AWS / GCS / ECMWF 直 を列挙し、
  **アジアからは GCS を推奨**とコメントする（選ぶのはユーザー、の原則は不変）。

### 必要フィールドのみの Range 取得

`.index`（1 ステップ約 40KB、フィールドごとの `_offset`/`_length` を記載）を読み、
GRIB2 本体は `Range: bytes=` で必要フィールドだけ取る。

実測（IFS 0.25° oper・2026-07-04 00z）:

| 取り方 | サイズ |
|--------|--------|
| 1 ステップ全変数の GRIB2 丸ごと | 136MB |
| MSL 1 面（Range） | **0.52MB** |
| gh500 / t850 1 面 | 0.40 / 0.59MB |
| 3 変数 × 3 ステップ | 4.5MB（丸ごとなら約 400MB） |

### 実装上の必須事項（実測で判明）

1. **503 バックオフ**: 2→4→8 秒の指数バックオフ＋リトライ（AWS だけでなく
   GCS でも安全側として実装する）
2. **index のキャッシュ**: 同一ステップの index を複数フィールドで再取得しない
   （素朴な実装は index 連打で即 503 を招いた）
3. **ステップ間引きの既定**: 表示既定は 6 時間刻み（〜120h で 21 ステップ）。
   全ステップはユーザー明示操作時のみ

## 2. ERA5 気候値 — このタスクだけ GCP で実行する

**設計判断: 気候値の計算は GCP（Colab）上で行い、日本へは成果物のみ持ち帰る。**

- 自前計算の転送量: 30 年 × 日別 × 0.25° 全球 ≈ **45GB/変数**（日本に引く量ではない）
- Colab は GCS の隣なのでデータセンター帯域で読める

### 方式 A（推奨）: WeatherBench2 の計算済み気候値

Google Research が ERA5 の時別気候値を公開している（存在・中身とも実測確認済み）:

```
gs://weatherbench2/datasets/era5-hourly-climatology/1990-2019_6h_1440x721.zarr
  dims: (hour: 4, dayofyear: 366, latitude: 721, longitude: 1440, level: 13)
  vars: mean_sea_level_pressure, 2m_temperature, geopotential, temperature ほか
```

- 再計算ゼロ。サブセット＋変換のみ（Colab で数分）
- **参照期間は 1990–2019**（WMO 標準 1991–2020 ではない）。図のラベルには必ず
  「vs 1990-2019」と明記（`climatology-analysis` スキルの Labels 規約）

### 方式 B: WMO 1991–2020 を厳密に使う場合

ARCO-ERA5（`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`）
から 12UTC 日代表 → `groupby dayofyear` → 31 日平滑。時間チャンク=1 のため
読み込みが重く、**必ず GCP 側で**。

### 成果物（気候値パック）

`notebooks/06-era5-climatology-gcp.ipynb` が生成する。

- 形式: NetCDF、**CF int16 パッキング**（scale_factor/add_offset。xarray で開けば
  自動復元、往復誤差ゼロを実測）＋ zlib
- サイズ: 0.5° 日別で **≈122MB/変数**（msl 実測換算）
- 命名: `{var}_{ref}_{smoothing}.nc` — アプリの気候値キャッシュ
  `~/.cache/aiseed-weather/climatology/` にそのまま置ける（skill のキー規約と一致）
- 配布: 一度作ればすべての利用者が計算不要。「1 人が GCP で作り、みんなに配る」

## 3. 共同化（会員/コミュニティ）の位置づけ

データ取得の権利は不要（すべて無料・CC-BY-4.0 系）。共同化が意味を持つのは
**下流の成果物**:

1. 気候値パックの配布（上記。ホスティングは GitHub Releases / R2 無料枠から）
2. ラン別の地域パック（日本域 crop + int16。3 変数 × 3 ステップで
   0.34MB を実測 = Range 取得比 1/13、丸ごと比 1/1000）— 利用者が増えたら
3. 作った図・注釈テンプレのギャラリー（このアプリの本質は共有）

費用が発生し始めた段階で寄付/会費に移行するのが順番。データのための会員制は
法的にも（非公開カタログは再配布禁止）経済的にも成立しない。

## 4. JMA レイヤーの検証済み知見（WeatherStatic プロジェクトからの移転）

姉妹プロジェクト WeatherStatic（気温と雨量の統計サイトの静的化）で 2026-07-05 に
実測検証した、本アプリの JMA レイヤーにも直接効く事実:

| 項目 | 内容 |
|------|------|
| 予報 office の統合例外 | `area.json` に存在しても forecast JSON が 404 の office がある: **014030（十勝）→ 014100、460040（奄美）→ 460100**。鹿児島 460010 → 460100 |
| jmatile のタイル座標 | 512px タイル。**タイル番号 = 256px スキームの floor(worldpx/512)**（=256 版の半分）。z は偶数のみ実在・maxNativeZoom あり（推計気象分布は 10） |
| 推計気象分布（suikei）の凡例色 | 晴れ (255,170,0) / くもり (170,170,170) / 雨 (0,65,255) / 雨または雪 (160,210,255) / 雪 (242,242,255) — 凡例 SVG から取得し実況照合済み |
| amedastable の elems フラグ | [0]=気温 [1]=降水量 [2,3]=風 [4]=気圧（官署） [5]=積雪 [6]=日照 [7]=海面気圧（官署）— map JSON と突合して実測特定 |
| アメダス番号の性質 | **改番が多い**ため主キーにしない。統計側の地点番号（官署=国際地点番号 5 桁、アメダス=etrn 4 桁）は歴史的にも転用ゼロで安定（1678 地点で検証） |
| CORS | bosai 系（amedas/forecast/jmatile）は `Access-Control-Allow-Origin: *` — ブラウザ直接取得可 |
| アメダス日平均の定義 | 毎正時 24 回平均。map JSON の毎正時値から**公式値と完全一致**の日平均を自前計算できる（東京 4 日分で検証） |

詳細な出典・検証ログは WeatherStatic リポジトリの `DATA_SOURCES.md` /
`DATA_CONTRACT.md` を参照。

## 5. WeatherStatic との関係（リポジトリ構成の方針）

WeatherStatic は「特定サイトの静的ジェネレータ＋運用データ」であり、本リポジトリ
（公開・AGPL・デスクトップアプリ）とは製品もライフサイクルも異なるため、
**丸ごとの統合はしない**。共有するのは JMA アクセス層の知見とコード:

- 短期: 本書 §4 のように知見を文書で移転（済み）
- 中期: `weatherlib/jma.py`・`suikei.py`・`etrn.py` 相当を本リポジトリの
  `services/` に取り込む際は、上記の検証済み挙動をテストとして固定する
- WeatherStatic を公開する場合は aiseed 配下の**別リポジトリ**とし、
  公開前にライセンス付与・`store/`（投票ハッシュ・観測蓄積）や `master/raw/` の
  git 除外・出典表記の整備を行う
