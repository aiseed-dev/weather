# Flet で業務ソフトを書く ― 現代版 Visual Basic としての実例

> 気象データ可視化アプリ `aiseed-weather` を題材に

「Python だけで」「宣言的に」「クロスプラットフォームの GUI を書く」 ―
**Flet** はこの三つを本気で実現していて、業務ソフトの開発用 UI として、
Visual Basic 6 が当時持っていた生産性を、現代のスタックで取り戻している。

この記事では抽象論はやめて、実際の中規模アプリ `aiseed-weather`
（気象庁・ECMWF・Open-Meteo を統合した天気図スタジオ、Flet 0.85 +
Python 3.13 製、コンポーネント 6,800 行、図形描画 3,200 行）を例に、
業務 GUI として Flet が何を解決しているかを見ていく。

---

## アプリの構成

```
src/aiseed_weather/
├── components/        # Flet コンポーネント (UI)
│   ├── app.py             ← ルート + ナビゲーション
│   ├── point_forecast_view.py  ← 地点予報タブ (2,000 行)
│   ├── map_view.py             ← 天気図タブ (3,300 行)
│   ├── radar_view.py           ← レーダータブ
│   └── amedas_view.py          ← AMeDAS 観測タブ
├── figures/           # matplotlib + flet.canvas でのチャート描画
├── services/          # ECMWF / ERA5 / JMA / Open-Meteo へのアクセス
└── models/            # データクラス / 設定
```

UI ・データ取得・データ処理がすべて **Python で書かれている**。
Web/REST API のレイヤがなく、polars の DataFrame をそのまま Flet
コンポーネントに渡している。

---

## 1. 宣言的コンポーネント ― React Hooks 風が Python ネイティブ

Flet 0.70+ の宣言的モードでは、`@ft.component` でデコレートした関数が
コンポーネントになり、`ft.use_state` / `ft.use_effect` / `ft.use_ref`
といったフックを React 同様に使う。

`aiseed-weather` の地点予報ビュー (実コードから抜粋):

```python
@ft.component
def PointForecastView(settings: UserSettings):
    data_dir = resolved_data_dir(settings)
    locations, set_locations = ft.use_state(load_locations(data_dir))
    selected_name, set_selected_name = ft.use_state(settings.default_location)

    forecast_state, set_forecast_state = ft.use_state("idle")
    forecast_data, set_forecast_data = ft.use_state(None)
    error_msg, set_error_msg = ft.use_state("")

    variable, set_variable = ft.use_state("temperature_2m")
    visible_days, set_visible_days = ft.use_state(7)
    pan_offset_h, set_pan_offset_h = ft.use_state(0)
    # … (実コードでは 30+ の use_state スロット)
```

地点リスト、選択中の地点、ロード状態、予報データ、エラーメッセージ、
表示変数、表示期間、パンオフセット… **業務 UI のすべての状態を、
ローカルな `use_state` だけで扱えている**。Redux も Context Provider も
Zustand も登場しない。

```python
    async def fetch_forecast(_):
        set_forecast_state("loading")
        try:
            df = await fetch_open_meteo(lat, lon, ...)
            set_forecast_data(df)
            set_forecast_state("ready")
        except Exception as e:
            set_error_msg(str(e))
            set_forecast_state("error")

    return ft.Column([
        ft.Dropdown(
            value=selected_name,
            options=[ft.DropdownOption(loc.name) for loc in locations],
            on_change=lambda e: set_selected_name(e.value),
        ),
        ft.FilledButton("更新", on_click=fetch_forecast),
        _StatusBanner(forecast_state, error_msg),
        _HourlyStrip(forecast_data) if forecast_data else None,
        _Chart(forecast_data, variable, visible_days, pan_offset_h),
    ])
```

これが **コンポーネントの全貌**。VB の `Button1_Click` がそのまま
`on_click=` に置き換わっていると思えばよい。`async def` のハンドラが
そのまま受理されるところが、現代的な改善点。

---

## 2. リアクティブな共有状態 ― `@ft.observable`

タブをまたいで生きる「長時間ダウンロード処理」のような状態には、
`@ft.observable` 付きのデータクラスを使う。これは `aiseed-weather` の
天気図タブが、ECMWF GRIB2 ファイル（1 ファイル数十 MB）を S3 から
並列ダウンロードする間、進捗を画面上のあらゆる場所に伝播させるための
仕組み:

```python
@ft.observable
@dataclass
class FetchSession:
    """ECMWF Open Data ダウンロードのライフサイクル状態。
    タブ移動しても生き続け、すべてのコンポーネントが購読する。"""
    running: bool = False
    items: list = field(default_factory=list)
    progress: dict = field(default_factory=lambda: {"done": 0, "total": 0})
    status_text: str = ""
```

`session.running = True` と書くだけで、これを `use_state` で読んでいる
全コンポーネントが自動再描画される。**React の Recoil / Jotai 風だが、
追加のライブラリ無しで Flet 標準で出来る**。

業務アプリで頻発する「裏で長いジョブが走っているときに、画面の
ステータスバーとプログレスバーとボタンの enable/disable が連動する」を、
配線コードゼロで実現できるのが効く。

---

## 3. Python だけで vector chart を描く ― `flet.canvas`

業務アプリで頻出する「自前グラフ」 ― `aiseed-weather` では当初
matplotlib を `ft.Image` に貼り付けていたが、ホバーやクリックを後付け
したい都合で、`flet.canvas` で描き直した。これが **650 行で 5 変数 ×
HRES + MSM + 気候値 + アンサンブル帯の重ね描き**ができる:

```python
import flet.canvas as cv

shapes: list[cv.Shape] = []

# 気候値の範囲帯 (p25 〜 p75)
shapes.append(cv.Path(
    elements=band_path_elements,
    paint=ft.Paint(color="#d8e4f5", style=ft.PaintingStyle.FILL),
))

# HRES 折れ線
shapes.append(cv.Path(
    elements=hres_line_elements,
    paint=ft.Paint(color="#234b86", stroke_width=2.0,
                   style=ft.PaintingStyle.STROKE),
))

# 軸ラベル
for tick in time_ticks:
    shapes.append(cv.Line(x, pad_t, x, pad_t + plot_h,
                          paint=ft.Paint(color="#cccccc")))
    shapes.append(cv.Text(x, pad_t + plot_h + 14, label,
                          style=ft.TextStyle(size=10)))

return cv.Canvas(shapes=shapes, width=W, height=H)
```

**Web フロントエンドだったら d3.js / Recharts / Plotly + データ整形
コードが必要なところを、すべて Python で完結している**。データソースの
polars DataFrame から直接 Canvas の `Path` 要素を組み立てている。
「データ処理と描画の言語が同じ」というのが、業務開発でじわじわ効いて
くる。

---

## 4. 出力用は別経路 ― matplotlib も同じコードベースで

画面表示は `flet.canvas` だが、「PNG ダウンロード」ボタンを押したときの
出力は matplotlib で生成している:

```python
fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(times, temperatures, label="HRES")
ax.fill_between(times, p25, p75, alpha=0.3, label="気候値範囲")
fig.savefig(path, dpi=150, metadata={"Source": "ECMWF Open Data / ..."})
```

**インタラクティブ用とエクスポート用で別エンジン**、しかし両方 Python
なので「変数追加」「色の統一」が一箇所で済む。Web アプリで「画面は
React + 出力は別バックエンドで Pillow」みたいな分裂が起きないのが
大きい。

---

## 5. ネイティブの非同期 I/O ― `asyncio` がそのまま動く

ECMWF の S3 からの GRIB2 ダウンロードはネットワーク律速で並列化したい。
`asyncio.TaskGroup` + `httpx.AsyncClient` を、Flet のイベントハンドラ
から直接呼ぶ:

```python
async def on_fetch(_):
    session.running = True  # 全コンポーネントが自動更新
    try:
        async with asyncio.TaskGroup() as tg:
            for param in params:
                tg.create_task(download_one(param, session))
    except* asyncio.CancelledError:
        session.status_text = "キャンセル"
    finally:
        session.running = False
```

VB 6 時代の `DoEvents` 地獄も、Electron の IPC 配線地獄もない。
**ネイティブ Python の async が、UI の更新サイクルと自然に噛み合って
いる**。

---

## 6. ネイティブ依存も問題なし ― miniforge / conda-forge

業務アプリは「Python から C ライブラリを呼ぶ」が頻発する。
`aiseed-weather` は:

- **cartopy** ― 地理投影法 (C++ + PROJ + GEOS)
- **cfgrib** ― GRIB2 読み込み (eccodes / C ライブラリ)
- **xarray + dask** ― 多次元配列処理

これらは pip では設定が辛いが、miniforge を使えば `environment.yml`
一発で揃う:

```yaml
dependencies:
  - python=3.13
  - flet>=0.85
  - polars
  - xarray
  - cfgrib
  - cartopy
  - matplotlib
```

```bash
mamba env create --prefix ./.venv -f environment.yml
mamba activate ./.venv
flet run src/aiseed_weather/main.py
```

**Flet と科学計算スタックが衝突しない**。これは Electron 系では地獄に
近い領域 (各 OS のネイティブビルド) で、Python + conda の組み合わせの
強さがそのまま生きる。

---

## 7. 配布もコマンド一発

```bash
flet pack src/aiseed_weather/main.py  # 単一バイナリ
flet build apk                         # Android
flet build web                         # PWA
```

同じコードベースから、デスクトップアプリ、Web アプリ、モバイルアプリを
生成できる。**業務 SI の世界では「同じツールを社内 Web と現場タブレット
で」が頻出**するので、これは事実上の killer feature。

---

## 8. 組み込み・業務専用端末にも刺さる

Flet が業務 GUI で強いのはデスクトップ / Web だけではない。
**Flutter ベースのレンダリングが GPU を素直に使う**ため、ARM ボード級の
ハードでもヌルヌル動く。これは業務向け専用端末を作る現場で重要な性質。

### 想定される用途

- **工場 HMI** (生産ライン横のタッチパネル)
- **倉庫ハンディ端末** (Android タブレット + ピストルスキャナ)
- **POS / 受付端末 / キオスク**
- **検査装置のオペレータパネル** (秤・カメラ・PLC との連動)
- **車載・船舶のセカンダリ表示**
- **実験室の計測機 GUI** (シリアル / GPIB / Modbus 経由)

### なぜ Flet が向くのか

| 要件 | Flet の対応 |
|---|---|
| Raspberry Pi 4/5 等で動く | Flutter Linux desktop が動けば OK。実績多数 |
| タッチ前提 UI | Flutter / Material はタッチファースト設計 |
| キオスク化 (フルスクリーン、Window chrome なし) | `page.window.full_screen = True` 1 行 |
| ハードと話す (GPIO / シリアル / CAN / Modbus / OPC-UA) | Python 業務ライブラリがそのまま使える (`pyserial`, `pymodbus`, `python-can`, `asyncua` …) |
| 画像処理・推論を同居 | OpenCV, PyTorch, ONNX Runtime, TFLite すべて Python から |
| 現場ごとのカスタマイズ | コード 1 行差し替えて再配置、ビルドチェーン不要 |
| 監視を Web からも見たい | 同じコードで `flet run --web` ― 端末側と遠隔監視ダッシュボードを 1 ソースで |

### `aiseed-weather` の例から類推できること

このアプリは「PC で動く解析ツール」として書かれているが、**全く同じ
コードを Raspberry Pi 5 + 7" タッチディスプレイで動かしてキオスクモード
にすれば、それだけで気象表示端末になる**。

```python
def main(page: ft.Page):
    page.window.full_screen = True
    page.window.frameless = True
    page.theme_mode = ft.ThemeMode.DARK  # 夜間視認性
    page.padding = 0
    page.add(App())

ft.run(main)
```

3 行追加するだけで「業務専用端末」のガワが整う。matplotlib のグラフは
Pi 5 でも 60 fps スムーズに描かれる。これが Flet の地味だが強烈な利点。

### 既存スタックとの比較

| | Qt for Python (PySide6) | Tkinter | LVGL (C) | Electron + Web | **Flet** |
|---|---|---|---|---|---|
| 学習コスト | 高 (Qt 用語) | 低だが古臭い | 高 (C/組み込み) | 中 (Web スタック) | **低** |
| 商用ライセンス | LGPL 解釈に注意 | OK | OK | OK | **MIT** |
| タッチ前提設計 | △ | × | ◎ | ○ | **◎** |
| Pi 4/5 で軽快 | ○ | ○ | ◎ | × (重) | **○** |
| Python の科学計算と直結 | ○ | ○ | × | △ | **◎** |
| 同ソースで Web 監視も | × | × | × | △ | **◎** |
| 起動時間 | 中 | 速 | 即時 | 遅 | 中 |

**特筆すべきは「端末用ファームと、それを遠隔監視する Web ダッシュボード
が同一ソース」になる点**。業務 IoT で監視 Web をわざわざ別チームで作って
きた現場には、開発体制ごと変える破壊力がある。

### 弱点

- メモリ消費は Flutter ランタイム分が乗る (組み込み Linux で常駐
  RAM 200 MB 程度〜)。MCU クラスは無理 ― そこは LVGL の領分
- 画面サイズ 320×240 のような極小ディスプレイは想定外
- ハードリアルタイム性が要る制御ループは別プロセスに分けるのが定石
  (UI は Flet、制御は別の Python プロセスや C で書いて IPC)

これらを承知の上で言うと、**「Raspberry Pi 級以上 + タッチパネル +
業務ロジック」のゾーンは Flet の独擅場になりつつある**。

---

## 9. ボトルネックは Rust で潰せる ― PyO3 / maturin

業務アプリで「Python だと特定の処理だけ遅い」場面は必ず来る。画像処理
の per-pixel ループ、座標変換、文字列パース、専有プロトコルのデコード
― そういう局所的ホットスポットだけ Rust に逃がす道が、現在の Python
では極めて簡潔に整っている。

### 最小限の手順

```bash
pip install maturin
maturin new --bindings pyo3 myfast
cd myfast
```

`src/lib.rs`:

```rust
use pyo3::prelude::*;
use rayon::prelude::*;

#[pyfunction]
fn smooth(values: Vec<f32>, window: usize) -> Vec<f32> {
    // 並列移動平均 ― rayon が物理コアを使い切る
    (0..values.len()).into_par_iter().map(|i| {
        let lo = i.saturating_sub(window);
        let hi = (i + window + 1).min(values.len());
        values[lo..hi].iter().sum::<f32>() / (hi - lo) as f32
    }).collect()
}

#[pymodule]
fn myfast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(smooth, m)?)?;
    Ok(())
}
```

```bash
maturin develop --release   # ビルド + 現在の venv にインストール
```

これだけで Python 側から `import myfast; myfast.smooth(arr, 5)` できる。
**C 拡張のような `setup.py` 地獄も、`pybind11` のテンプレートメタ
プログラミング地獄もない**。

### `aiseed-weather` で言えば

このアプリは `_precompute_coastlines.py` で海岸線データを事前処理して
`.npz` に固めている ― つまり「重い処理は事前計算で前借りする」戦略を
取っている。これでも足りなくなったら、自然な次の一手が Rust 拡張:

- **海岸線クリッピング** ― 数十万線分 × 投影変換、Python のループでは
  厳しい
- **GRIB2 デコードのカスタム後処理** ― 配列の bit-pack 解除や擬似カラー
  変換
- **インタラクティブ画面でのリアルタイム再投影** ― マウスドラッグごと
  に数十万ポイントの座標計算

これらを丸ごと書き直す必要はない。**該当関数 1 つだけを Rust で書いて
差し替える**ことができる。Python 側のコードは
`from aiseed_fast import reproject` と書き換えるだけ。

### Rust エコシステムの恩恵

| 用途 | Crate |
|---|---|
| データ並列 | `rayon` |
| 多次元配列 | `ndarray` (numpy と相互運用) |
| numpy 配列を直接受け渡し | `numpy` (PyO3 公式バインディング) |
| SIMD | `packed_simd`, `std::simd` (nightly) |
| 画像処理 | `image`, `imageproc` |
| 地理座標変換 | `proj`, `geo` |
| Modbus / OPC-UA / CAN | `tokio-modbus`, `opcua`, `socketcan` |

特に **numpy 配列を Rust 側でゼロコピーで受け取れる**点が業務的に大きい。
pandas / polars / xarray の中身は全部 numpy 配列なので、データ処理
パイプラインの「**ある一段だけ Rust**」が現実的に実行できる。

### 既に Rust の恩恵を受けている部分

実は `aiseed-weather` のスタックは、明示的に Rust を書いていなくても
**既に Rust の上に乗っている**:

- **polars** ― Rust 製の DataFrame エンジン (pandas より高速)
- **ruff** ― Rust 製の lint / formatter
- **uv** ― Rust 製のパッケージマネージャ
- **pydantic v2** ― コア部分が Rust

つまり「最初から Rust を間接利用していて、必要に応じて直書きにも降り
られる」というのが現在の Python 生態系の標準的な姿。**Flet で UI、
polars で処理、自前のホットスポットだけ PyO3 で Rust** ― この三層構造
が業務アプリの安定解になる。

### 既存代替との比較

| 手段 | 学習コスト | ビルド配布 | numpy 連携 | 並列処理 |
|---|---|---|---|---|
| Cython | 中 | 面倒 (C コンパイラ依存) | ◎ | GIL 解放が要工夫 |
| pybind11 (C++) | 高 | 同上 | ◎ | C++ の並列化が要 |
| ctypes | 低 | 自前ビルド要 | △ (型変換が手書き) | × |
| numba | 低 | 不要 (JIT) | ◎ | ◎ |
| **PyO3 + maturin** | **低〜中** | **`pip install` 可能な wheel** | **◎** | **`rayon` で 1 行** |

numba は手軽だが、関数ローカルの最適化に限られる (型推論できる純数値
計算向き)。**業務ロジック (パーサ、I/O、複雑な分岐) を含む処理は Rust
の方が素直に書ける**。

### 弱点

- Rust 自体の学習コスト ― 借用 / ライフタイムは最初は重い
- ビルド時間 (とはいえ業務クラスのクレートで数十秒)
- デプロイ環境に Rust ツールチェインが必要 (ビルド時のみ。配布は wheel)

`aiseed-weather` のようにすでに conda-forge で C ライブラリ群をビルド
している現場では、**Rust ツールチェインの追加負荷はゼロに近い**
(`conda install rust` で済む)。

---

## Visual Basic との対比 ― 何が継承され、何が現代化されたか

| | VB 6 | Flet + Python (+ 任意で Rust) |
|---|---|---|
| 学習コスト | 低い | 同じく低い (Python が読めれば 1 日で動かせる) |
| GUI 配置 | フォームデザイナ | コード (Git, レビュー, AI 生成と相性◎) |
| イベント駆動 | `Button1_Click` | `on_click=fn` (async OK) |
| 状態管理 | グローバル変数 + フォームスコープ | `use_state` + `@ft.observable` |
| 数値計算 | 自前 / Excel 連携 | numpy / polars / xarray が標準 |
| 可視化 | MSChart, ActiveX | matplotlib / `flet.canvas` / Plotly |
| ネットワーク | WinINet, Winsock | `httpx`, `asyncio`, gRPC, etc. |
| 高性能ホットスポット | × (VB の限界) | **PyO3 で Rust 化、polars / numpy で間接利用** |
| 配布 | EXE | EXE / Web / iOS / Android / 組み込み端末 |
| 動作環境 | Windows only | Win / Mac / Linux / Web / Mobile / Raspberry Pi |
| エコシステム | 業務 ActiveX | Python パッケージ全部 + Rust crate |

VB 6 で生産性を支えていたのは「**言語の単純さ × フォームの即時性 ×
配布の単純さ**」だった。**Flet はこの三拍子を、Python という強力な裏方
を引き連れて現代に再演している**。そして VB が当時カバーできなかった
「**高性能数値処理**」「**ネイティブ並列**」「**全プラットフォーム配布**」
のすべてが、Flet + PyO3 / polars / numpy の組み合わせで補完される。

---

## こんな現場に向く

`aiseed-weather` を書きながら強く感じた、Flet の本領が出る場面:

1. **データを扱う社内ツール** ― 解析・集計の中心が pandas / polars
   で書かれていて、それを業務担当者に GUI で配りたい
2. **ML 推論を業務に乗せたい** ― モデル推論 (PyTorch / scikit-learn)
   を呼ぶ画面を、データサイエンス担当者自身が書ける
3. **科学計算 + 可視化** ― matplotlib / Plotly の表現力をそのまま画面
   に埋め込める
4. **VB / VBA / Access からの脱却** ― 言語の生産性を落とさず
   クロスプラットフォーム化したい
5. **Electron / Tauri で挫折した** ― JS スタックの面倒さを払拭したい
6. **Streamlit より本格的なものを書きたい** ― 状態の自由度、UI
   コンポーネントの選択肢、配布形態の幅が決定的に違う
7. **業務専用端末・組み込み HMI** ― Raspberry Pi / Linux ボード +
   タッチパネルで動く、現場据え置きの専用 UI を、Python で書きたい
   (シリアル / Modbus / GPIO 連携込み)

---

## 帳票 / 印刷も実は強い ― Excel テンプレート方式

業務アプリで避けて通れない「帳票出力」 ― これは Flet の弱点ではなく、
むしろ Python の伝統的な得意領域。日本の現場で de facto となっている
**Excel 帳票**を、テンプレートに値を流し込む方式で生成できる:

```python
from openpyxl import load_workbook

wb = load_workbook("template/月次報告.xlsx")
ws = wb["集計"]
ws["B3"] = report_date
ws["D5"] = total_count
for i, row in enumerate(df.iter_rows(named=True), start=10):
    ws.cell(i, 1, row["地点"])
    ws.cell(i, 2, row["最高気温"])
    ws.cell(i, 3, row["最低気温"])
wb.save(output_path)
```

書式・罫線・印刷範囲・ヘッダ/フッタ・社判の画像はテンプレート側で
作り込んでおけば、Python は値を埋めるだけ。**現場の経理 / 事務担当が
Excel で直接テンプレートを編集できる**ので、運用に乗せやすい。

選べる出力経路:

| 出力 | ライブラリ | 用途 |
|---|---|---|
| Excel (.xlsx) テンプレート差し込み | `openpyxl` | 月次報告、明細、見積書 |
| Excel + チャート / 条件付き書式 | `xlsxwriter` | グラフ付き集計表 |
| Word (.docx) テンプレート | `python-docx`, `docxtpl` | 契約書、報告書 |
| PDF (低レベル) | `reportlab` | レイアウト固定の証憑 |
| HTML → PDF | `weasyprint` | CSS で組んだレポート |
| プリンタ直送 | `win32print` (Win), `cups` (Linux) | ラベル発行、レシート |

**Flet コンポーネント側はボタン 1 つで済む**:

```python
ft.FilledButton(
    "帳票出力",
    icon=ft.Icons.PRINT,
    on_click=lambda _: export_excel(data, settings.template_path),
)
```

`aiseed-weather` の「PNG ダウンロード」と同じパターン ― 画面表示と
出力ファイルで別エンジンを使い、両方とも Python なので 1 つのコード
ベースに収まる。

---

## 成熟ステータス ― 宣言的モードは production-ready 宣言済み

執筆時点で、公式 (Flet Team) は **宣言的モード = 実アプリで使える段階**
だと明言している:

- **Flet 0.85** (公式ブログタイトル: *"Declarative apps grow up —
  Router, dialogs, and more"*) で、`@ft.component` を本気で本番に
  使うために最後まで欠けていた **ルーティング (`ft.Router`)** と
  **宣言的ダイアログ (`ft.use_dialog()`)** が追加された。ネスト
  ルート、レイアウト + outlet、動的セグメント、データローダ、
  ネイティブ view-stack 連携 (`manage_views=True`) 込み
- **Flet 0.83** リリースノートで「API は 99% 安定、1.0 までほぼ変わら
  ない」とアナウンス済み
- **Flet 1.0 Alpha → Beta** がすでに公開されており、1.0 stable が
  視野に入っている (`aiseed-weather` は 0.85 系を使用)

つまり、**「新しい API だから様子見」というフェーズはもう終わっている**。
今から新規業務アプリを書き始めるなら、宣言的モード一択。

注意点としては「Web 上の古い記事は命令的 API (`page.add()` / 直接ミュ
テート) で書かれているものが多い」 ― 検索ヒットを鵜呑みにせず、公式
ドキュメントの "Declarative" セクションに合わせて読み替える必要が
ある (この記事のサンプルも当然宣言的モード)。

---

## 弱点も正直に

- `DataTable` は 1 万行スクロールには向かない。仮想化は自前
- モバイル向けの細かい UX (ジェスチャ等) は Flutter 知識が要る
- 配布バイナリは Flutter ランタイム同梱で最小 30 MB から
- Web 上の入門記事は古い命令的 API のものが多く、最新の宣言的モード
  ベースの情報は公式ドキュメント中心に当たる必要がある

**それでも、業務 GUI を Python で書くという軸がもたらす利得が、これら
を上回る場面は広い**。

---

## まとめ ― 「Python の上の Visual Basic」が今ここにある

`aiseed-weather` は、Flet が「中規模の本格業務アプリ」を支えられるか
どうかの個人的な検証だった。**結論は、はっきり YES**。

- 6,800 行の UI コンポーネント
- 3,200 行のチャート描画 (`flet.canvas` + matplotlib)
- 5 つのデータソース (ECMWF / ERA5 / JMA / Open-Meteo / dynamical.org)
- 非同期ダウンロード、キャンセル、再試行
- 出版品質の PNG/PDF エクスポート

これらを、**Python 単一言語**で、**Web のフロント / バックエンド分離
なし**で、**React も TypeScript もバンドラも CSS フレームワークも使わず**
に、ふつうに開発できている。必要になれば Rust にも降りられる。
Raspberry Pi にも載る。Web にも出せる。

VB 6 が「業務 GUI のミニマム生産性スタック」を発明したとすれば、
**Flet はそれを Python の文脈で再発明した上で、配布先と性能の天井を
取り払った**。VB の生産性を懐かしむすべての開発者、Streamlit で
物足りなさを感じているすべてのデータサイエンティスト、Electron で
挫折したすべての Python 使い、現場端末を Qt で苦労して作ってきた
すべての SI エンジニアに、`flet run` してみてほしい。

公式: <https://flet.dev/> ／ ソース: `aiseed-weather` リポジトリ
