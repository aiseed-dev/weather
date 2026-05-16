# 天気予報仕様

## 概要

過去・現在・未来の連続した天気時系列に、過去 30 年の平年値(平均と標準偏差を含む統計量)、および現在予報の不確実性(アンサンブル)を重ねて表示する個人用デスクトップアプリ。Flet 0.85+ の宣言的スタイルで実装。Open-Meteo の無料 API のみを使用。

公開されている天気予報サービスは「未来予報」「過去観測」「平年値」「予報の不確実性」を分断して提供している。本アプリは、これらを一画面で統合表示する。個人が AI と協働して作るからこそ可能な統合。

## 統合の意味

四つの異なる情報を、同じ時間軸上に重ねる:

1. **予報値の線** ── ECMWF IFS HRES 9km の決定論的予報(過去〜未来連続)
2. **平年値の帯** ── 過去 30 年の同日同時刻の平均と標準偏差(全期間に重ねる)
3. **アンサンブルの帯** ── 現在予報の不確実性(未来側のみ)
4. **参考予報の線**(日本国内のみ) ── JMA MSM 5km(短期前後)

これにより、ユーザーは次の判断ができる:

- 予報値が平年値の帯から外れる → 異常気象の予想
- 平年内だがアンサンブルの幅が広い → 予報が揺れている、まだ確定的でない
- 平年外でアンサンブルの幅が狭い → モデルが自信を持って異常を予報

公開サービスは「最も可能性の高い予報」を断定的に出すだけで、これらの構造的判断材料を提供しない。

## データソース

すべて Open-Meteo の無料 API(非商用、API キー不要、CC-BY 4.0)。

| 用途 | エンドポイント | モデル | 解像度 | 期間 | 永続化 |
|---|---|---|---|---|---|
| 主予報 | `/v1/forecast` | `ecmwf_ifs` | 9km、1h | 過去〜未来 15 日 | しない |
| 参考予報(日本のみ) | `/v1/forecast` | `jma_msm` | 5km、1h | 過去〜未来 4 日 | しない |
| 予報の不確実性 | `/v1/ensemble` | `ecmwf_ifs025` | 25km、3h | 未来 15 日 | しない |
| 過去データの蓄積 | `/v1/archive` | デフォルト(ERA5) | 9km、1h | 過去 30 年(蓄積) | する |

永続化するのは過去データ(ERA5)のみ。予報・参考予報・アンサンブルは毎回取り直して使い、保存しない。

### 主予報: ECMWF IFS HRES 9km

- 世界最高品質のグローバル決定論的予報
- `past_days` パラメータで過去データも同じ呼び出しで取得可能
- 全世界対応

### 参考予報: JMA MSM 5km

- 日本気象庁のメソスケールモデル
- 日本国内では HRES より局地的精度が高い場合がある
- 緯度経度が日本国内の場合のみ取得

### 予報の不確実性: ECMWF IFS ENS

- 51 メンバーのアンサンブル予報
- 各時刻について平均、スプレッド、分位数を計算して表示用に使う
- 過去のアンサンブルメンバーは保存しない(意味がない、起動時に取り直す)
- 解像度は HRES より低い(0.25°)が、不確実性の評価には十分

### 過去データ蓄積: ERA5

- ERA5 再解析、9km 解像度
- 1940 年から取得可能、本アプリでは直近 30 年を蓄積
- 蓄積した生データから、平均・標準偏差・分位数・歴代記録などを動的計算

## 主要な設計判断

### 状態管理
- Flet 0.85+ の宣言的スタイル(`@ft.component`、`use_state`、`use_dialog`)
- 外部状態管理ライブラリ(FletX 等)は使用しない
- フレームワーク内蔵のフックで完結させる

### データの持ち方
- 構造型データ(`dataclass`、辞書、リスト)で持つ
- `Control` オブジェクトを state に入れない
- ロジックと UI を関数単位で分離

### 永続化と永続化しないものの分離
- 永続化: 過去データ(ERA5)のみ → Parquet、月別ファイル
- 永続化しない: 予報、参考予報、アンサンブル → メモリ上のみ
- 場所のリスト: JSON(件数が少なく、手で確認・編集できる)

### 集計済みではなく生データを保存
- 過去データは集計せず生のまま Parquet に保存
- 平均、標準偏差、分位数などは Polars で動的計算
- 生データを保存しているため、後から任意の統計量を取り出せる(歴代記録、長期傾向など)
- これは「未来の自分への投資」── 今は使わない統計でも、必要になった時に取り出せる

### 列指向ストレージ
- Parquet を採用(列指向、圧縮効率が高い、Polars と相性が良い)
- データの性質(時系列・数値主体・列指向クエリ)と一致する

### 非同期処理
- すべて `async def` で書く
- API 呼び出しは `httpx` の `AsyncClient`
- 重い初回平年値構築は、進捗を `yield` で UI に反映
- Polars のクエリは軽いので、メインで実行して問題ない

## データ構造

### 場所 (Location)

```python
@dataclass
class Location:
    name: str          # 表示名(例: "徳島", "Paris")
    latitude: float
    longitude: float
    is_japan: bool     # 日本国内なら True(MSM 取得対象判定用)
    created_at: datetime
```

### 気象データの行(Parquet スキーマ)

```python
historical_schema = {
    "timestamp": pl.Datetime("us", time_zone="UTC"),
    "year": pl.Int32,
    "month": pl.Int8,
    "day": pl.Int8,
    "hour": pl.Int8,
    "temperature": pl.Float32,
    "precipitation": pl.Float32,
    "relative_humidity": pl.Float32,
    "wind_speed": pl.Float32,
    "cloud_cover": pl.Float32,
    "weather_code": pl.Int16,
}
```

型を明示することでファイルサイズが小さくなり、後の処理も安全になる。

### 予報データ(メモリ上のみ、Polars DataFrame)

予報・参考予報・アンサンブルは、API 取得後 Polars DataFrame として扱い、永続化しない。

## データの配置

```
~/.weather-app/
├── locations.json
└── data/
    ├── 徳島/
    │   ├── 1996-05.parquet
    │   ├── 1996-06.parquet
    │   ├── ...
    │   └── 2026-05.parquet
    └── Paris/
        ├── 1996-05.parquet
        └── ...
```

- 場所ごとにディレクトリ
- 月別 Parquet ファイル(`YYYY-MM.parquet`)
- 保存先のディレクトリは Flet の `page.storage_paths.get_application_documents_directory_async()` で取得した適切な場所

### 月別ファイルを採用する理由

- 毎日の更新で、触るファイルが最小化される(対象月だけ書き直す)
- 「ある月の全年データ」を効率的に読める(`*-05.parquet` のグロブ)── 平年値計算向き
- ファイルサイズが揃い、扱いやすい
- 時系列順にファイル名が並ぶ

## 過去データの取得戦略

### 初回(場所追加時)

現在日の前後 15 日について、過去 30 年分を取得して保存。

- データポイント数: 30 年 × 24 時間 × 15 日 = **10,800**
- API コール数: 約 30(年ごとに分けて取得)
- 所要時間: 1〜2 分(API 応答時間による)

擬似コード:

```python
async def build_initial_archive(location: Location, today: date):
    """場所追加時の初回構築。現在日の前後 15 日を過去 30 年分。"""
    window_dates = [today + timedelta(days=d) for d in range(-7, 8)]
    # 前後合わせて 15 日

    for years_ago in range(1, 31):
        target_year = today.year - years_ago
        # その年の同じ前後 15 日を取得
        start = date(target_year, ...) - timedelta(days=7)
        end = date(target_year, ...) + timedelta(days=7)
        data = await fetch_archive(location, start, end)
        await append_to_monthly_parquet(location, data)
        yield (years_ago, 30)  # 進捗を UI に返す
```

UI 側はこの `yield` を受けて進捗バーを更新する。

### 日々の更新

アプリ起動時、保存済みデータの範囲を確認し、不足分を取得。

- 通常は「翌日 1 日分」を 30 年分取得して追加
- データポイント数: 30 年 × 24 時間 = **720**
- API コール数: 1〜2

```python
async def update_archive(location: Location, target_date: date):
    if await has_data_for(location, target_date, years=30):
        return  # 既に揃っている
    for years_ago in range(1, 31):
        year_date = date(target_date.year - years_ago, target_date.month, target_date.day)
        data = await fetch_archive_single_day(location, year_date)
        await append_to_monthly_parquet(location, data)
```

データは累積保存(古いデータは消さない)。使い続けるうちに、全年分の過去データが揃っていく。

### 取得対象の変数

主予報、参考予報、過去データすべてで以下の変数を取得:

- `temperature_2m`
- `precipitation`
- `relative_humidity_2m`
- `wind_speed_10m`
- `cloud_cover`
- `weather_code`

アンサンブルでも同じ変数を取得(精度の評価用)。

## 統計と不確実性の計算

### 過去の変動(historical variability)

蓄積した Parquet から Polars で動的計算。

```python
import polars as pl
from pathlib import Path

def calculate_historical_stats(location_name: str, month: int, day: int) -> pl.DataFrame:
    """ある場所、ある月日の時刻別統計量を、過去 30 年分から計算。"""
    pattern = Path(f"~/.weather-app/data/{location_name}/*-{month:02d}.parquet").expanduser()
    df = pl.read_parquet(pattern)
    return (
        df.filter(pl.col("day") == day)
          .group_by("hour")
          .agg([
              pl.col("temperature").mean().alias("temp_mean"),
              pl.col("temperature").std().alias("temp_std"),
              pl.col("temperature").median().alias("temp_median"),
              pl.col("temperature").quantile(0.25).alias("temp_q25"),
              pl.col("temperature").quantile(0.75).alias("temp_q75"),
              pl.col("temperature").min().alias("temp_min"),
              pl.col("temperature").max().alias("temp_max"),
              pl.col("temperature").count().alias("sample_count"),
              # 他の変数も同様
          ])
          .sort("hour")
    )
```

### 予報の不確実性(forecast uncertainty)

アンサンブルから計算。51 メンバーのデータから、各時刻について平均、標準偏差、分位数を出す。

```python
def calculate_ensemble_stats(ensemble_df: pl.DataFrame) -> pl.DataFrame:
    """アンサンブルの各時刻について統計量を計算。"""
    return (
        ensemble_df
          .group_by("timestamp")
          .agg([
              pl.col("temperature").mean().alias("temp_ens_mean"),
              pl.col("temperature").std().alias("temp_ens_spread"),
              pl.col("temperature").quantile(0.1).alias("temp_ens_p10"),
              pl.col("temperature").quantile(0.9).alias("temp_ens_p90"),
              # 他の変数も同様
          ])
          .sort("timestamp")
    )
```

### 重要: 二つの統計の意味の違い

UI で両者を区別して表示する:

- **過去の変動(灰色の帯、点線)**: 「いつもはこれくらいの範囲」── 過去の事実
- **予報の不確実性(青色の帯、薄い塗り)**: 「予報がどれくらい揺れている」── 現在のモデルの自信度

両者を同じグラフに重ねることで、構造的な評価ができる。

## 機能仕様

### 必須機能

1. **場所の追加**
   - 緯度経度を直接入力するダイアログ(`use_dialog`)
   - 表示名を任意入力
   - 「日本国内かどうか」は緯度経度から自動判定(lat: 24〜46、lon: 122〜146 の矩形)
   - 追加すると、初回過去データ構築が始まる(進捗表示)

2. **場所の選択**
   - 登録済み場所のドロップダウン
   - 選択すると、その場所の予報を表示

3. **主表示: 過去〜未来の連続表示**
   - ECMWF IFS HRES の予報
   - `past_days=3, forecast_days=15` で取得
   - 表示変数: 気温、降水量、風速(初期表示)
   - 時系列のテーブルまたはシンプルなグラフ

4. **平年値の重ね表示**
   - 同月日同時刻の過去 30 年の統計量を SQL 的に計算
   - 平均と標準偏差から帯を作成
   - サンプル数が少ない時刻は、データ蓄積中であることを示す

5. **アンサンブルの不確実性表示**
   - 起動時に Ensemble API から取得
   - 51 メンバーから平均・スプレッド・分位数を計算
   - 未来側に帯として表示

6. **参考表示: MSM(日本国内のみ)**
   - 場所が日本国内の場合、画面下部に MSM の予報を並列表示

7. **データ更新**
   - 「更新」ボタンで Forecast API と Ensemble API を再取得
   - 起動時に自動で実行
   - 必要に応じて過去データの不足分も取得

### 任意機能(優先順位順)

1. **平年差・Z スコアの数値表示**
   - 各時刻について「予報値 − 平均」と「Z = (予報値 − 平均) / 標準偏差」を表示
   - 「+3.2σ(記録的)」「+1.2σ(やや暖かい)」など

2. **複数場所の登録・切替**

3. **グラフ表示**
   - 時系列グラフ
   - 三つの帯(平年値、アンサンブル、現在の値)を重ねる

4. **異常値のハイライト**
   - Z スコアに応じた色分け
   - +3σ 超は「記録的」として強調

5. **歴代記録の表示**
   - 「過去 30 年の最高気温」「最低気温」「最大降水量」
   - 予報値が記録に近づいた時に強調

6. **長期傾向の表示**
   - 直近 10 年の平均と過去 30 年の平均の差
   - 局所的な気候変動の検出

7. **表示日数の調整**
   - 過去 1/3/7 日、未来 3/7/15 日 の切替

8. **単位の切り替え**
   - 摂氏/華氏、km/h・m/s

9. **場所の編集・削除**

10. **データのバックアップ・復元**

### スコープ外

- 警報・注意報(JMA の公式情報、API では取得しない)
- レーダー画像、衛星画像
- 通知機能、バックグラウンド更新
- モバイル対応(Flet で技術的には可能だが、本仕様ではデスクトップに集中)
- 認証、ユーザー管理
- クラウド同期
- 多言語対応(日本語表示を基本とする)

## 画面構成

### メイン画面(単一画面、ルーティングなし)

```
┌─────────────────────────────────────────────────┐
│ 天気予報                                          │
│ ┌─────────────┐ [更新] [+ 場所追加]              │
│ │ 徳島 ▼      │                                 │
│ └─────────────┘                                 │
├─────────────────────────────────────────────────┤
│ ECMWF IFS HRES 9km                              │
│                                                 │
│ ┌─ 過去 ─┐│┌─ 未来 ─────────────────┐           │
│ │       │ │                                    │
│ │       │ │  ╱╲    平年値の帯(灰)              │
│ │       │ │ ╱──╲   アンサンブルの帯(青)        │
│ │   ●━━━●━━━●━━●  予報値(線)                │
│ │       │ │                                    │
│ │       現在                                   │
│ └────────────────────────────────────┘          │
├─────────────────────────────────────────────────┤
│ 参考: JMA MSM 5km(日本国内のみ表示)             │
│ ┌────────────────────────────────────┐          │
│ │時 気温 ...                         │          │
│ └────────────────────────────────────┘          │
└─────────────────────────────────────────────────┘
```

### 場所追加ダイアログ(`use_dialog` 使用)

- 表示名(テキスト入力)
- 緯度(数値入力)
- 経度(数値入力)
- 追加ボタン → 初回過去データ構築が始まる(進捗表示)
- キャンセルボタン

### 初回過去データ構築中(進捗表示)

- 「過去データを構築中... (5/30 年)」のような進捗
- 完了するとメイン画面に遷移

## API レート制限

Open-Meteo 無料層:
- 10,000 コール/日
- 5,000 コール/時
- 600 コール/分

このアプリの想定コール数:
- 初回過去データ構築: 場所一つあたり 30 コール(一度きり)
- 日々の更新: 場所一つあたり 1〜3 コール/日(過去 + 予報 + アンサンブル)

無料層の上限に対して、十分余裕がある。

## プロジェクト構造

```
weather-app/
├── .agents/
│   └── skills/
│       ├── flet-component-basics/
│       │   └── SKILL.md          # 既存
│       └── (open-meteo スキルは後で追加)
├── SPEC.md                       # このファイル
├── main.py                       # エントリーポイント
├── components/                   # Flet コンポーネント
│   ├── __init__.py
│   ├── app.py                    # ルート @ft.component
│   ├── location_selector.py
│   ├── weather_main_view.py
│   ├── weather_reference_view.py
│   └── add_location_dialog.py
├── data/                         # データ層
│   ├── __init__.py
│   ├── storage.py                # ファイル配置の管理
│   ├── locations.py              # JSON での場所管理
│   ├── archive.py                # Parquet の読み書き(月別)
│   └── stats.py                  # Polars での統計量計算
├── api/                          # 外部 API
│   ├── __init__.py
│   ├── forecast.py               # Forecast API(主予報、MSM)
│   ├── ensemble.py               # Ensemble API
│   └── archive.py                # Historical Weather API
└── pyproject.toml
```

## 依存ライブラリ

```
flet >= 0.85
httpx
polars >= 1.0
```

`sqlite3` は使わない。
外部の状態管理ライブラリは使用しない。

## 起動

```bash
flet run
```

## 実装の順序(推奨)

各ステップで動くものを残しながら進める。

1. **プロジェクト構造の作成、Parquet ディレクトリ準備**
2. **Forecast API の呼び出し**(まず主予報だけ、場所は固定値)
3. **メイン画面の最小実装**(取得した予報をテーブル表示)
4. **場所の登録・選択機能**(JSON での永続化、ドロップダウン)
5. **Historical Weather API の呼び出し、初回過去データ構築**(進捗表示込み)
6. **Polars での統計量計算**(平均と標準偏差から帯を作成)
7. **平年値の表示**(予報と並べる、テーブルから)
8. **MSM 並列表示**(日本国内判定、画面下部に追加)
9. **Ensemble API の呼び出し、不確実性の表示**
10. **グラフ表示への移行**(三つの帯を重ねる)
11. **任意機能**(優先順位順)

## 参考リンク

- Open-Meteo Forecast API: https://open-meteo.com/en/docs
- Open-Meteo Historical Weather API: https://open-meteo.com/en/docs/historical-weather-api
- Open-Meteo Ensemble API: https://open-meteo.com/en/docs/ensemble-api
- Open-Meteo JMA API: https://open-meteo.com/en/docs/jma-api
- Open-Meteo ECMWF API: https://open-meteo.com/en/docs/ecmwf-api
- Flet 0.85+ ドキュメント: https://flet.dev