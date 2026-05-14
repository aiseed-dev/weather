# AIseed Weather

**A weather chart studio for enthusiasts and analysts.**
Build publication-ready figures from ECMWF, ERA5, JMA, and Open-Meteo, then
share them with the people who need to see them.

> "Bring Your Own Data" — this tool visualizes published meteorological data.
> It does not produce or distribute forecasts of its own.

## Who this is for

This is **not** a general-purpose weather app. It is for people who:

- Read pressure charts, jet stream maps, and 500 hPa geopotential plots
- Want to compare current conditions against ERA5 climatology (1940-present)
- Need to check Japan's current radar and AMeDAS observations
- Need to produce annotated figures to explain weather events to others
- Find existing public charts too limited in variables or styling

If you do not know what MSL, geopotential height, or anomaly means, this tool
will not be friendlier than any other. That is by design — we optimize for
expert workflow, not onboarding.

## What it does

- **Global forecast maps** from ECMWF Open Data (IFS and AIFS)
- **Climatology / anomaly maps** from ERA5 (1940-present)
- **Japan rainfall nowcast** from JMA radar tiles
- **Japan ground observations** from JMA AMeDAS (~1,300 stations)
- **Multi-layer composition**: pressure isobars, temperature fields, wind,
  precipitation, geopotential at any pressure level
- **Annotation**: text labels, arrows, region highlights for explanation
- **Export**: PNG and PDF with embedded attribution and provenance metadata
- **Animation**: across forecast steps or historical date ranges
- **Point forecasts** from Open-Meteo as a supporting view

## Two principles you should know about

**1. The user chooses data sources.** Source selection lives in
`~/.config/aiseed-weather/config.toml`. The first launch writes a commented
template there for you to edit. Pick which ECMWF mirror, which ERA5 access
path, and whether to enable Open-Meteo, then restart. The app never
preselects and never shows a setup UI. JMA is per-feature (no config key
needed; JMA endpoints are public and free).

**2. Data fetches only on user actions.** Opening a view, pressing Refresh, or
changing a parameter triggers a fetch. The app never polls in the background,
never auto-updates a displayed value, never pre-fetches. If the cache is
fresh enough (per the source's update cadence), it is used; otherwise the
view shows a progress indicator and fetches.

## Status

Early development. Skeleton, services, conventions, and navigation are in
place. Next milestone: render a single MSL chart from a live ECMWF run.

## Stack

- [Flet](https://flet.dev/) — declarative Python UI
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
  via AWS S3 mirror (`s3://ecmwf-forecasts`) — primary data source
- [ERA5](https://registry.opendata.aws/ecmwf-era5/) via AWS (`s3://ecmwf-era5`)
  — climatology and historical reference
- [JMA](https://www.jma.go.jp/) public endpoints — Japan radar and AMeDAS
- [Open-Meteo](https://open-meteo.com/) — supporting point forecasts
- xarray + cfgrib for GRIB2 decoding
- matplotlib + cartopy for map rendering

## Setup (Miniforge — required)

Cartopy、cfgrib、eccodes は C ライブラリ (PROJ, GEOS, eccodes) に依存します。
これらは pip で入れるとプラットフォームごとに苦労するため、conda-forge から
入れます。

### Step 1: Miniforge のインストール

https://github.com/conda-forge/miniforge から OS に合ったインストーラーを
ダウンロードして実行します。

```bash
# Linux / macOS の場合
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash Miniforge3-$(uname)-$(uname -m).sh
```

インストール時の質問には基本的に Enter で進めて構いません。最後に
「Do you wish to update your shell profile to automatically initialize conda?」
と聞かれたら **yes** を選びます。シェルを再起動して `mamba --version`
または `conda --version` で確認します。

### Step 2: base 環境の自動起動を無効化

インストール直後の状態では、ターミナルを開くたびに `(base)` 環境が自動で
アクティブになります。これはシステムの Python と conda の Python が混ざる
原因になるので、無効化しておきます。

```bash
conda config --set auto_activate_base false
```

これでターミナルを開いても base が起動せず、必要なときだけ
`mamba activate <環境名>` で明示的にアクティブにできます。

### Step 3: プロジェクト専用の仮想環境を作成

AIseed Weather は**プロジェクトディレクトリ内に `.venv` として環境を作る**
方式を取ります。グローバル環境を汚さず、リポジトリと環境が一緒に管理できる
ため、別マシンへの移動や VS Code との連携がスムーズです。

```bash
# リポジトリをクローン
git clone https://github.com//aiseed-weather.git
cd aiseed-weather

# プロジェクト内に .venv を作成 (Python 3.13 ベース)
mamba env create --prefix ./.venv -f environment.yml
```

`environment.yml` の内容(C ライブラリ依存パッケージは conda-forge から、
Flet は pip 経由)がそのまま `.venv` にインストールされます。

### Step 4: 環境のアクティブ化

```bash
mamba activate ./.venv
```

`--prefix` で作った環境は、名前ではなくパスでアクティブにします。プロンプトに
`(/path/to/aiseed-weather/.venv)` のように表示されれば成功です。

### Step 5: プロジェクトを editable install (初回のみ)

```bash
pip install -e .
```

`src/aiseed_weather/` 配下のパッケージを Python から `import` できるように
します。`-e` (editable) なのでファイル編集は再インストール不要でそのまま
反映されます。`src/` レイアウト (PyPA 推奨) を採用しているため、この一手間が
必要です。

### Step 6: 起動

プロジェクトルートで:

```bash
flet run
```

開発中はホットリロード付きで:

```bash
flet run -r
```

`pyproject.toml` の `[tool.flet.app] path` 設定により、`flet run` はプロジェクト
ルートから `src/aiseed_weather/main.py` を自動的に見つけます。

### 環境の更新と削除

`environment.yml` を変更した後は、

```bash
mamba env update --prefix ./.venv -f environment.yml --prune
```

で同期します。`--prune` は yml から削除された依存を環境からも消すオプション
です。

環境を完全に作り直したい場合は、

```bash
mamba env remove --prefix ./.venv
mamba env create --prefix ./.venv -f environment.yml
```

`.venv` ディレクトリは数百MB〜数GBになるので、`.gitignore` で除外されています。

### Why Python 3.13?

Flet のモバイル対応が Python 3.13 で公式サポートされた PEP 730 (iOS) と
PEP 738 (Android) を前提としているため、3.13 を採用しています。

### Why Flet via pip instead of conda?

Flet はベータ段階で頻繁にリリースされるため、conda-forge が追いつかない
場合があります。最新版を直接管理するため pip 経由でインストールします。
conda が Flet を知らない状態にすることで、conda + pip の競合を回避します。

### Why `--prefix ./.venv` instead of named environments?

通常の `mamba env create -n my-env` は、conda のグローバル管理ディレクトリ
(`~/miniforge3/envs/`)に環境を作ります。これに対し `--prefix ./.venv` は
プロジェクトディレクトリ内に環境を作るため、

- リポジトリと環境が一緒に移動できる
- VS Code や PyCharm が `.venv` を自動認識する
- プロジェクトを削除するときに環境も一緒に消える
- 複数のプロジェクトで同名の環境を作っても衝突しない

というメリットがあります。

### Windows と macOS について

このプロジェクトの公式サポートは Linux (Debian/Ubuntu 系) です。動作確認も
Linux で行っています。

**macOS**: Miniforge for macOS をインストールすれば、上記の手順がほぼそのまま
動きます。cartopy や cfgrib も conda-forge が macOS バイナリを用意しているため、
追加の作業は不要です。

**Windows**: Miniforge for Windows をインストールすれば動作する可能性が
ありますが、公式の動作確認外です。Windows でデスクトップ用途として本格的に
使いたい場合は、Linux への移行を検討することをお勧めします。Windows 10 の
サポート終了 (2025年10月) を機に、古い PC を Linux に切り替える人が増えて
います。

WSL (Windows Subsystem for Linux) は本アプリには推奨しません。WSL は CLI や
サーバプロセス向けで、デスクトップ GUI アプリでは描画の遅延、ファイル
ダイアログの不整合、日本語入力の不安定さなどの問題があります。

## License

- Code: **AGPL-3.0-or-later**
- ECMWF data: CC-BY-4.0
- Open-Meteo data: CC-BY-4.0
- JMA data: 出典: 気象庁ホームページ — processed-data notice appears on
  composited figures (radar overlays, AMeDAS station maps)

The export feature automatically embeds attribution and the data run
identifier in every output, so figures shared from this tool carry their
provenance.

## Project layout

```
src/aiseed_weather/
├── main.py
├── components/                       # Flet components (UI only)
│   ├── app.py                        # nav between map / radar / amedas
│   ├── map_view.py                   # ECMWF/ERA5 synoptic charts
│   ├── radar_view.py                 # JMA rainfall nowcast
│   └── amedas_view.py                # JMA ground observations
├── services/                         # data fetching, decoding (no Flet imports)
│   ├── forecast_service.py           # ECMWF Open Data
│   ├── point_forecast_service.py     # Open-Meteo
│   ├── jma_radar_service.py          # JMA radar tiles
│   ├── jma_amedas_service.py         # JMA AMeDAS
│   └── jma_endpoints.py              # URL registry
└── models/                           # dataclasses, observable models
    └── user_settings.py
```

## For contributors and AI agents

Read `CLAUDE.md` first, then `AGENTS.md`, then the relevant skills under
`.agents/skills/`. The skills encode this project's conventions and the
prioritization between data sources.
