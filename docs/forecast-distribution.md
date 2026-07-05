# 数値予報の配信分割 設計書（サーバー処理 → Cloudflare → 表示）

作成: 2026-07-06。ステータス: **承認済み**（ユーザー決定: 領域は全球・
ステップは全部・気圧面も全部、よく使うセットを優先作成）。

## 目的

現在のアプリは各利用者が ECMWF Open Data を直接取得する（1 ステップ約 150MB
× ステップ数）。これを次の 2 つに分割する:

1. **publisher（サーバー処理）** — ECMWF から取得し、日本周辺だけを切り出して
   小さなパックに変換し、Cloudflare R2 へ配置する。**当分はユーザーが手元の
   マシンで実行する**（cron でも手動でも可）
2. **表示側** — アプリは ECMWF ではなく Cloudflare からパックを取得して描画する

効果: ECMWF への負荷は「利用者数 × 150MB×25」から「**1 × それ**」に減り、
利用者側の転送は 1 ステップ約 150MB → **数 MB** になる。R2 は配信転送料が
恒久無料なので、利用者が何人いても配信費用は発生しない。
`docs/data-acquisition.md` §3 で述べた「共同化が意味を持つのは下流の成果物」の
実装であり、WeatherStatic の dist/ と同じ「静的分割配置」モデルの予報版。

## 全体像

```
[ユーザーのマシン（当分）]                 [Cloudflare R2]              [各利用者のアプリ]
publisher CLI                            forecast/
  ECMWF GCSミラーから bulk GET      →      latest.json            ←    mirror ソース
  日本周辺crop + int16パック化              runs/{run}/                  （取得ボタンで fetch）
  manifest生成・古いラン削除                  manifest.json               ↓
                                            {step}h-sfc.nc          既存 figures/ で描画
                                            {step}h-pl.nc           （コードは無変更）
```

## 1. パック仕様

- **形式**: NetCDF-4、フィールドごとに CF int16 パッキング
  （scale_factor/add_offset。ERA5 気候値パックで往復誤差ゼロを実測済み）＋ zlib
- **変数名**: cfgrib がデコードした後の xarray 変数名をそのまま使う
  （`msl`, `t2m`, `u10`, `v10`, `tp`, `gh`, `t`, …）。**figures/ が読む名前と
  一致させることで描画コードを無変更にする**のがこの設計の要
- **領域**: **全球**（ユーザー決定）。crop なし、0.25° 1440×721 のまま
- **ステップ**: **全部**（IFS oper: 0–144h は 3 時間刻み、150–240h は
  6 時間刻み、計 65 ステップ）
- **収録**: sfc・pl とも実装済み全変数。ただし**二段公開**（ユーザー決定:
  よく使うセットを優先作成）:
  - **core**（先に全ステップ分を公開）: sfc = `msl, t2m, u10, v10, tp, tcc` /
    pl = `gh@500, gh@300, t@850, t@500, u/v@850, u/v@250, r@700, w@700`
    — 地上天気図・500hPa 高度・850hPa 気温風・700hPa 湿数/鉛直流・250hPa ジェット
    という定番の組
  - **ext**（core 公開後に追記）: 残りの全変数×全気圧面
- **ファイル分割**: `{step:03d}h-{kind}-{tier}.nc`（kind ∈ sfc/pl、tier ∈
  core/ext）。kind 分割は data-flow スキルに一致し、tier 分割で「先に使える」
  を実現する。manifest に tier ごとの完成フラグを持つ
- **属性**: run・step・model・`Data: ECMWF Open Data … CC-BY-4.0` を
  グローバル属性で埋め込む（再配布は CC-BY-4.0 で合法。出典表示は figures/footer が
  既に描く）

サイズ実測（2026-07-05 12Z ラン +24h、全球 0.25°）:

| パート | 実測 | 中身 |
|---|---|---|
| sfc-core | 7.2MB | msl, t2m, u10, v10, tp, tcc |
| pl-core | 34.7MB | gh/t/u/v/r/w × 250/300/500/700/850hPa |
| sfc-ext | 27.9MB | 残り 30 変数（CAPE・放射・積雪・海況ほか） |
| pl-ext | 58.5MB | z/q/vo/d × 全 14 面（1000〜10hPa） |
| pl-ext-lv | 54.6MB | core 変数の残り 9 面 |
| sol-ext | 7.2MB | 土壌層 |

→ **core ≈ 42MB/步（1 ラン 2.7GB）、全部 ≈ 190MB/步（1 ラン 12.4GB）**。
GRIB 自体が高圧縮なので int16+zlib での縮小は限定的。全部×2 ラン保持は
R2 無料枠 10GB を超え、超過分 約15GB × $0.015 ≈ **月 $0.2**（表示側の
転送は無料のまま）。無料に収めるなら「core 2 ラン＋ext 最新 1 ラン」。
往復精度の実測: msl 最大誤差 0.08Pa、t2m 0.0006K、gh500 0.08gpm —
表示用途では実質ロスレス。
publisher の取得量は 1 ラン 65×約150MB ≈ **10GB**（GCS ミラー実測 14MB/s で
約 15–25 分。core→ext の 2 パス間は grib-cache に GRIB を保持するため
一時ディスクも最大 約10GB）。1 日 4 ラン全部ではなく 00z/12z の 2 ラン運用を推奨。

## 2. R2 レイアウト

```
forecast/
  latest.json                 # {"model":"ifs","run":"2026-07-06T00Z","base":"runs/20260706_00z",
                              #  "steps":[0,3,...,240],"tiers":{"core":true,"ext":false}}
  runs/20260706_00z/
    manifest.json             # 全ファイルの bytes / sha256、変数一覧、tier 完成フラグ
    000h-sfc-core.nc  000h-pl-core.nc  000h-sfc-ext.nc  000h-pl-ext.nc
    003h-sfc-core.nc  ...
```

- publisher は **core を全ステップ置き終えた時点で** latest.json を
  `tiers.core=true` で差し替え（この時点で利用者は定番チャートを使える）、
  ext 完了後に `tiers.ext=true` へ更新する（段階公開・置き途中は見せない）
- 保持は既定 **2 ラン**。それより古い runs/ は publisher が削除

## 3. publisher（サーバー処理・当分ユーザー実行）

`tools/publish_forecast.py`（リポジトリ直下 tools/ を新設。src/ のアプリ本体とは
分離するが、`services/forecast_service.py` の取得ロジックと catalog の変数定義を
import して再利用する。Flet には依存しない）。

```
python tools/publish_forecast.py --out ./publish \
    [--steps 0:144:6] [--model ifs] [--runs-keep 4] [--region 95,180,0,65]
```

処理: 最新ラン解決（ecmwf-opendata の latest() プローブ）→ ステップごとに
bulk GET（GCS ミラー・既存の 503 バックオフ）→ cfgrib デコード → crop →
int16 パック → `./publish/forecast/…` に書き出し → manifest / latest.json 生成
→ ローカル側でも古いランを掃除。

**アップロードは分離**する（ビルドと本番反映を分ける website の運用原則と同じ）。
書き出した `publish/` を上げる手段は次のどれでもよい:

```
rclone sync publish/forecast r2:<bucket>/forecast   # 推奨（差分同期）
wrangler r2 object put ...                          # 単発
cf-publish r2 sync（v0.2 実装後）
```

## 4. 表示側 — アプリの mirror ソース

- config.toml に選択肢を追加（**ユーザーが明示的に選ぶ。既定にはしない**）:

```toml
[forecast]
source = "mirror"                     # 既存: aws / gcp / azure / ecmwf / none
mirror_url = "https://<R2公開URL>/forecast"
```

- 新サービス `services/mirror_forecast_service.py`:
  `latest_run()` は latest.json を読むだけ、`download(step, kind)` は
  該当 .nc を GET してキャッシュ（`~/.cache/aiseed-weather/mirror/…`）。
  ForecastService と同じ呼び出し面を持たせ、コンポーネント側は分岐 1 箇所
- **取得は従来どおり「取得ボタン」でのみ**（user-action-fetch の規則は不変。
  変わるのは取得先とサイズだけ）
- mirror が落ちていたら**エラーをそのまま見せる**。ECMWF 直接へ黙って
  フォールバックしない（aiseed-conventions の禁止事項）
- figures/ は無変更。footer の出典表示に「via mirror」を含める

## 5. マイルストーン

| # | 内容 | 状態 |
|---|------|------|
| M1 | publisher: 取得→パック→ローカル publish/ 生成 | **完了**（2026-07-05 12Z 実ランの step 0/24 で検証。往復精度は上表、パックから render_msl 描画確認） |
| M2 | アプリ mirror ソース | **完了**（`forecast_source = "mirror"` + `mirror_url`。ローカル HTTP 配信で E2E: latest_run → kind 別 download → render_layer で msl/t2m/wind10m/gh500 描画。既存テスト 8 passed） |
| M3 | R2 反映手順（実アップロードはユーザー） | 未。`rclone sync publish/forecast r2:<bucket>/forecast` が基本形 |

### ext 取得トグル（実装済み）

取得確認ダイアログ（mirror 選択時のみ）に「ext も取得」チェックボックスを
追加した。既定オフ = core のみ（約 42+7MB/步）で定番チャート全部が動く。
オンにすると各 kind の ext siblings（sfc-ext / pl-ext / pl-ext-lv）も取得し、
decode が自動マージして全 43 サーフェス変数・全 14 気圧面が描ける
（E2E: d2m と t925 の実描画を確認）。mirror 側に ext が未公開（core 先行
公開の途中）のときは 404 を警告ログにして core のみで続行する。

なお **sol（土壌層）は core 扱い**とした。アプリの取得ループは常に
kind = sfc/pl/sol を取る（`_ACTIVE_KINDS`）ため、sol が ext だと mirror の
既定取得が毎回 404 になる。約 7MB/步と小さいので core に置く方が正しい。

### v1 の既知の制約

- 短サイクル（06/18z）の延長ホップ先が mirror に無い場合はエラー表示
  （publisher は 240h まで揃う 00/12z ランだけを公開するため実害は限定的）
- 進捗表示のサイズ集計は core ファイル分のみ（ext 取得分は加算されない。表示上の過小のみ）

## 6. 確認したい点

1. 領域は日本周辺 (95–180E, 0–65N) の 1 種でよいか（全球パックは 1 ラン
   数百 MB になるため対象外とする。GLOBAL 表示のときだけ従来どおり ECMWF 直接）
2. ステップは 6 時間刻み 0–144h でよいか（延長レンジ 150–240h は 6h 刻みが
   ないため v1 対象外）
3. pl（気圧面）も常に含めてよいか（含めると 1 ラン約 100MB、sfc のみなら約 20MB）
