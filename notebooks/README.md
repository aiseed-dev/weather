# JupyterLab サンプルノートブック

`aiseed-weather` のサービス層・キャッシュ・パレットを JupyterLab から
触るためのスタータ集。アプリ本体 (`flet run`) と同じ `./.venv` で動く。

> **English**: see [README.en.md](README.en.md)

## なぜ JupyterLab?

`aiseed-weather` は「GUI で天気図を作るスタジオ」だが、その裏側は
すべて Python の業務スタック (xarray / polars / cfgrib / cartopy /
matplotlib) で組まれている。**気になったことをその場で深掘りする**
には、GUI とは別の自由度の高い解析環境が要る ― それが Jupyter。

- キャッシュ済みの ECMWF GRIB2 を `xarray.open_dataset(...,
  engine="cfgrib")` で開いて自前の解析にかける
- Open-Meteo の polars DataFrame を任意の地点で取って比較する
- AMeDAS の最新観測を polars テーブルで見る
- アプリと同じパレットで独自のチャートを試作する

Electron 系アプリ では絶対に作れないこの自由度が、Python デスクトップ
アプリの最大の強み。

## 起動

`./.venv` をアクティブにした状態で:

```bash
jupyter lab notebooks/
```

(`flet run` を回せている環境なら追加準備は不要。`jupyterlab` /
`ipykernel` / `ipympl` は `environment.yml` に含まれている)

## ノート一覧

| # | ファイル | 内容 |
|---|---|---|
| 01 | [`01-quickstart.ipynb`](01-quickstart.ipynb) | 環境チェック、`data_dir` の所在、保存済み地点の確認 |
| 02 | [`02-ecmwf-grib2.ipynb`](02-ecmwf-grib2.ipynb) | キャッシュ済み ECMWF GRIB2 を `cfgrib` で開いて MSL を描画 |
| 03 | [`03-point-forecast.ipynb`](03-point-forecast.ipynb) | Open-Meteo で 3 都市 (徳島・札幌・那覇) を並列取得して比較 |
| 04 | [`04-jma-nowcast.ipynb`](04-jma-nowcast.ipynb) | AMeDAS スナップショット + 最寄観測点抽出、大都市の現況テーブル |
| 05 | [`05-custom-chart.ipynb`](05-custom-chart.ipynb) | プロジェクトのパレット LUT を使って独自チャートを描く |
| 06 | [`06-era5-climatology-gcp.ipynb`](06-era5-climatology-gcp.ipynb) | ERA5 気候値パックを GCP (Colab) 側で作る — このタスクだけは GCP で実行 |

順番に読み進める設計だが、各ノートは独立して動くので必要なところだけ
開いても OK。

## 共通の作法

### `await` の書き方

サービス層 (`open_meteo_forecast.fetch_forecast`,
`JmaAmedasService.fetch`, …) はすべて `async def`。Jupyter は
top-level `await` をそのままサポートしているので、`asyncio.run(...)`
で包む必要は無い:

```python
result = await fetch_forecast(latitude=33.78, longitude=134.49, client=client)
```

### データ帰属

このプロジェクトが扱うデータは公開元の規約に従って表示する必要がある:

- **ECMWF Open Data** → CC-BY-4.0、「出典: ECMWF Open Data, CC-BY-4.0」
- **Open-Meteo** → CC-BY-4.0、「出典: Open-Meteo (https://open-meteo.com)」
- **気象庁 JMA** → 「出典: 気象庁ホームページ」(加工データは「処理データ」と明記)

ノートブックで作った図を公開・共有する場合は、必ず帰属を入れる。
`05-custom-chart.ipynb` 末尾、および本体側の `figures/footer.py` 参照。

### キャッシュの場所

`aiseed-weather` のキャッシュは以下のいずれかに置かれる:

- `~/.config/aiseed-weather/config.toml` の `data_dir` を設定していれば、そこ
- 未設定なら `platformdirs.user_cache_dir("aiseed-weather")`
  (Linux: `~/.cache/aiseed-weather/`)

`01-quickstart.ipynb` の最初のセルで `resolved_data_dir(settings)` を
呼べば実際のパスが分かる。

## トラブルシューティング

### 「キャッシュが空」と出る

ECMWF / Open-Meteo のキャッシュは、**ユーザが明示的に何かを取得した
ときだけ**作られる (background fetch 無し)。アプリを一度 `flet run` で
起動し、地点を追加 → 更新ボタンを押すか、天気図タブで Refresh すれば
キャッシュが生成される。

### `cfgrib` で開けない

`environment.yml` 経由で `eccodes` (C ライブラリ) が入っているはず。
`pip` で `cfgrib` だけ入れた場合は eccodes が無いので失敗する。
miniforge / conda-forge 経由で環境を作り直す:

```bash
mamba env create --prefix ./.venv -f environment.yml
```

### `ipympl` のインタラクティブ表示が出ない

`%matplotlib widget` をセル冒頭に書く。JupyterLab を起動し直すと反映
されることがある。

## さらに踏み込む

- `src/aiseed_weather/services/` の各モジュールはそれぞれ独立して
  使えるよう設計されている。docstring を読むと API が分かる
- `src/aiseed_weather/figures/` にプロジェクトのチャート規約・パレット
  仕様がすべて入っている
- `.agents/skills/` にコーディング規約。Claude Code を使うときはこれが
  自動でロードされる
