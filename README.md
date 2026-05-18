# AIseed Weather

**愛好家とアナリストのための天気図スタジオ。**
ECMWF・ERA5・気象庁・Open-Meteo のデータから出版品質の図を作り、
必要な人と共有するためのツールです。

> 「Bring Your Own Data」 ― このツールは公開されている気象データを
> 可視化するためのもので、独自の予報を生成・配信するものではありません。

> **English**: see [README.en.md](README.en.md)

## 想定する利用者

これは汎用の天気アプリではありません。次のような人のためのツールです:

- 気圧図、ジェット気流図、500 hPa 等高度図を読み解く
- 現況を ERA5 気候値 (1940 年〜現在) と比較したい
- 日本のレーダーと AMeDAS 観測の現況を確認する必要がある
- 注釈付きの図を作って気象現象を他者に説明する必要がある
- 既存の公開チャートでは変数や見た目が物足りない

MSL、ジオポテンシャル高度、アノマリが何を意味するかご存じなければ、
このツールは他の天気アプリより親切ではありません。**専門ワークフロー
最適化が設計方針**で、入門のしやすさは目指していません。

## できること

- **全球予報図** ― ECMWF Open Data (IFS / AIFS)
- **気候値・アノマリ図** ― ERA5 (1940 年〜現在)
- **日本の雨量ナウキャスト** ― 気象庁レーダータイル
- **日本の地上観測** ― 気象庁 AMeDAS (約 1,300 地点)
- **多層合成** ― 等圧線、気温場、風、降水、任意の気圧面のジオポ
  テンシャル
- **注釈** ― テキストラベル、矢印、領域ハイライトによる説明補助
- **エクスポート** ― 帰属情報と起源メタデータを埋め込んだ PNG / PDF
- **アニメーション** ― 予報ステップ、または過去の日付範囲の連続表示
- **地点予報** ― Open-Meteo を補助ビューとして利用

## 知っておくべき 2 つの設計原則

**1. データソースはユーザが選ぶ。** ソース選択は
`~/.config/aiseed-weather/config.toml` で管理されます。初回起動時に
コメント付きテンプレートが自動生成されるので、それを編集して
「どの ECMWF ミラーを使うか」「ERA5 へのアクセス経路」「Open-Meteo
を有効にするか」を決め、再起動します。アプリ側で勝手に選ぶことも、
設定 UI を出すこともありません。気象庁は機能ごとに有効 (設定キー
不要 ― 公開・無料の API のため)。

**2. データ取得はユーザの操作時のみ。** ビューを開く・更新ボタンを
押す・パラメータを変更する、これらの操作でフェッチが走ります。
**バックグラウンドポーリングなし、自動更新なし、先読みなし**。
キャッシュが鮮度を保っていれば (各ソースの更新頻度に応じて) それを
使い、そうでなければ進捗インジケータを出してフェッチします。

## ステータス

初期開発段階。スケルトン、サービス層、規約、ナビゲーションが整備済み。
次のマイルストーン: 実 ECMWF ラン 1 本から MSL 図を 1 枚描画する。

## 技術スタック

- [Flet](https://flet.dev/) ― 宣言的 Python UI
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data) ―
  AWS S3 ミラー (`s3://ecmwf-forecasts`) 経由、主要データソース
- [ERA5](https://registry.opendata.aws/ecmwf-era5/) ― AWS (`s3://ecmwf-era5`)
  経由、気候値・過去参照
- [気象庁](https://www.jma.go.jp/) 公開エンドポイント ― 日本のレーダーと AMeDAS
- [Open-Meteo](https://open-meteo.com/) ― 地点予報の補助
- xarray + cfgrib ― GRIB2 デコード
- matplotlib + cartopy ― 地図描画

## セットアップ (Miniforge 必須)

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

### JupyterLab で解析する

サンプルノートブックが `notebooks/` に同梱されています ― ECMWF GRIB2 の
直接読み込み、Open-Meteo の並列取得、AMeDAS スナップショット、プロジェクト
パレットを使った独自チャートまで。`./.venv` をアクティブにした状態で:

```bash
jupyter lab notebooks/
```

詳細は [`notebooks/README.md`](notebooks/README.md) 参照。

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

### なぜ Python 3.13?

Flet のモバイル対応が Python 3.13 で公式サポートされた PEP 730 (iOS) と
PEP 738 (Android) を前提としているため、3.13 を採用しています。

### なぜ Flet は conda ではなく pip 経由?

Flet はベータ段階で頻繁にリリースされるため、conda-forge が追いつかない
場合があります。最新版を直接管理するため pip 経由でインストールします。
conda が Flet を知らない状態にすることで、conda + pip の競合を回避します。

### なぜ `--prefix ./.venv` 方式 (名前付き環境を使わない理由)?

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

## ライセンス

- コード: **AGPL-3.0-or-later**
- ECMWF データ: CC-BY-4.0
- Open-Meteo データ: CC-BY-4.0
- 気象庁データ: 出典: 気象庁ホームページ ― 合成データ (レーダー重ね描き、
  AMeDAS 地点マップ等) には「処理データ」表記を併記

エクスポート機能は帰属とデータラン識別子を自動で埋め込みます。本ツールから
共有された図は、起源情報を必ず持って出ていきます。

## プロジェクト構成

```
src/aiseed_weather/
├── main.py
├── components/                       # Flet コンポーネント (UI のみ)
│   ├── app.py                        # 地図 / レーダー / AMeDAS のナビ
│   ├── map_view.py                   # ECMWF/ERA5 総観チャート
│   ├── radar_view.py                 # 気象庁レーダー雨量ナウキャスト
│   └── amedas_view.py                # 気象庁 AMeDAS 地上観測
├── services/                         # データ取得・デコード (Flet 非依存)
│   ├── forecast_service.py           # ECMWF Open Data
│   ├── point_forecast_service.py     # Open-Meteo
│   ├── jma_radar_service.py          # 気象庁レーダータイル
│   ├── jma_amedas_service.py         # 気象庁 AMeDAS
│   └── jma_endpoints.py              # URL レジストリ
└── models/                           # データクラス、リアクティブモデル
    └── user_settings.py
```

## コントリビュータと AI エージェント向け

まず `CLAUDE.md`、続いて `AGENTS.md` を読んでください。その上で、該当する
Skill を `.agents/skills/` 配下から参照します。Skill 群にはプロジェクト規約と、
データソース間の優先度ルールがエンコードされています。
