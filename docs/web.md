# サイトの構成（個人開発気象統計）

3,132 ページ・112MB の静的サイト。Cloudflare Pages に置く。
運用手順は [operations.md](operations.md)。

## 誰のためのサイトか

**恐らく自分専用。** 普通の人にはデスクトップ版（AIseed Weather）を勧める
（`/App/` がその案内）。サイトを公開したままにしておくのは、

- 「気象庁の観測をどこまで個人が扱えるか」の実証であること
- 裏で動く収集の仕組みが、**アプリの読む R2 のデータも作っている**こと
  （サイトを止めてもデータ基盤は止められない）
- **アプリの文書の置き場**であること（`/App/` が紹介、`/App/Develop/` が
  開発マニュアル。内容の正本はリポジトリの README / AGENTS / CLAUDE で、
  ページはその写し）

の 3 つによる。閲覧者の多寡は設計の前提にしない。

## 何を載せて、何を載せないか

**気象庁の観測データだけ**を扱う。予報は載せない。予報業務には気象業務法の
許可が要るため。警報・注意報などの防災情報も扱わない。

出典表示は全ページのフッタに入る（`templates/_layout.html`）:

> 個人が開発・運用している非公式サイトです。気象庁の観測データを加工して
> 掲載しています（出典: 気象庁ホームページ）。**警報・注意報などの防災情報は
> 扱っていません。**

デスクトップ版（AIseed Weather）はこの制約を受けない。利用者が自分で
データ源を選び、自分の手元で取得・生成するため。予報を見たい人は
デスクトップ版を使う。

## ページの構成

**URL はすべて小文字**（下の「旧サイトの URL との互換」）。

| URL | 数 | 内容 | 作る人 | 更新 |
|-----|----|------|--------|------|
| `/` | 1 | いまの日本（動画のように場面が切り替わる: 全国の地図 → 都市めぐり → 全国のようす。設定はブラウザに保存）・このサイトでわかること（案内のカード）・主要都市の今日の記録（広い画面だけ） | generate | 10 分 |
| `/temperature/…` | 4 | 今日の最高・最低（主要都市／各地） | generate | 毎時 |
| `/summer/…` `/winter/…` | 8 | 暑さ・寒さのランキングと日数一覧（今年・今季） | generate | 毎時 |
| `/summer/…/{年}` `/winter/…/{年}` | 8 × 年 | 過去の年の同じページ（1881 年〜。冬は寒候年） | generate（`build_past_seasons`） | 年のデータが変わったとき |
| `/temperature/summerday/{a〜d}{年月日}` | 枠 1 | その日の猛暑日などの地点（任意の日） | generate（枠）＋ ブラウザ | データは毎時 |
| `/temperature/summermonth/{a〜d}/{月}` | 枠 1 | 月の日ごとの地点数（2010 年〜） | 同上 | 同上 |
| `/monthly/monthly(l)/{年月}` | 枠 1 | 月の平均気温のランキング（任意の月） | 同上 | 同上 |
| `/climate/chart/{地点}` | 896 | 雨温図（平年値の気温と降水量） | generate | 毎時 |
| `/stations/jp/{地点}` | 896 | 地点別の気候値・30 日推移・平年値 | generate | 毎時 |
| `/monthly/…` | 26 | 月別気温（観測値・平年値） | generate | 毎時 |
| `/precipitation/` | 1 | 降水量ランキング | generate | 毎時 |
| `/status/temperature/` `/wind/` `/precipitation/` `/snow/` | 4 | 実況（10 分値） | generate_status | 10 分 |
| `/status/station/{地点}` | 1,287 | 地点別の 10 分値 | generate_status | 10 分 |
| `/status/records/` | 1 | 観測史上 1 位の更新状況 | generate_status | 10 分 |
| `/status/lab/` | 1 | Python グラフ工房（ブラウザ内 Python） | generate_status | 10 分 |
| `/about/` `/app/` | 2 | サイト説明・デスクトップ版の案内 | generate | 毎時 |
| `/app/develop/` | 1 | **アプリの開発マニュアル**（環境構築・構成・スキル・設計原則） | generate | 毎時 |
| `/data/daily/` | 1 ＋ 配布ファイル | **日別の観測データ**（気温・降水・日照、1880 年〜、廃止地点を含む。年ごと・地点ごとの NetCDF） | generate（書き出しは export_dist） | ストアの更新時 |
| `/data/amedas/` | 1 ＋ 配布ファイル | **アメダス 10 分値アーカイブ**（半月ごとの NetCDF・索引・地点一覧） | generate | 10 分 |

### 過去の記録のページ（枠 ＋ 月ごとのデータ）

旧サイトの日ごと・月ごとのページは日付の数だけあり、1 枚ずつ作ると Pages の
上限（2 万ファイル）を超える。そこで種類ごとに枠を 1 枚（`/history/day/`・
`/history/month/`・`/history/monthly/`）作り、旧 URL は `_redirects` の **200**
（URL はそのままで行き先の中身を返す）で枠につなぐ。枠のスクリプト
（`assets/js/history.js`）が URL を読み、月ごとのデータを取ってきて表を描く。

- データは `public/data/history/`（`weatherlib/history.py` の `export`）。集計はここで
  済ませ、スクリプトは順位を付けて並べるだけ。形はモジュールの先頭に書いてある
  - `{年}/{月}.json` その月の日ごと・種類ごとの地点（大きい月で約 660KB、圧縮して約 180KB）
  - `{年}/monthly.json` 月の平均気温のランキング
  - `table/{種類}{月}.json` 月の日ごとの地点数
  - `stations.json` 地点番号 → 名前・府県・地点ページの URL 名
- 変わった年（とその翌年。連続日数が年をまたぐ）と今年だけ書き直す。年のファイルの
  大きさと更新時刻を `data/history_state.json` に控える。全部で 147 年・約 1,950 本・
  58MB、全部書き直しても deb2 で十数秒
- 過去の年の夏・冬のページは年ごとのファイル（`build_past_seasons`。今季と同じ
  `build_season_pages`）。年のデータか作り方が変わった年だけ作り直す
  （`data/past_seasons.json`）。観測した地点が無い年は作らない
- 資料不足値（品質 4 以下）は気象庁と同じ扱い: その日の一覧には「]」を付けて載せ、
  月平均・期間の極値・日数には使わない。日ごとのページに南鳥島と富士山は入れない

**10 分ごとに作り直すのは status 配下の 1,293 ページ**（43MB）。
残りは毎時。`generate.py` はサイト全体を、`generate_status.py` は Status
だけを受け持つ。分けているのは、10 分ごとに 3,132 ページを作り直すのが
無駄だから。

## データの配布（/data/amedas/）

気象庁のアメダス 10 分値は気象庁のサイトで約 9 日しか公開されない。
`fetch_amedas_mirror.py` が半月ごとに封入した NetCDF-4（`public_amedas/archive/`）を、
`generate.py` の `build_data_amedas` が説明ページ・機械向けの索引（`index.json`）・
地点一覧（`station/index.json`）と一緒に配る。

- ファイルの一覧と変数の表は、生成のたびに実物の NetCDF から作る
- 欠けた半月（収集が止まっていた期間）も一覧に出す。半月の最終日が取得窓（10 日）
  から外れるまでは「まとめる前（○日ごろ公開）」と出す（ミラーがそのときに封入する）
- 生の 10 分値 JSON（`map/`）は配らない。気象庁が配っている期間と重なるため
- 1 時間値（`hourly/{YYYY}/{MM}-{1|2}.nc`）は、10 分値の半月ファイルから毎正時の時刻だけを
  抜いたもの（`fetch_amedas_mirror.py` の `ensure_hourly`。封入の後に作る）。約 1/6 の大きさ
- 地点別データにしか無い要素（最大瞬間風速・日最高最低の起時）は、日別の値を
  月ごとの NetCDF（`daily/{YYYY}/{MM}.nc`）で配る（`weatherlib/pointdaily.py`）
- 1 ファイルは半月で約 15MB（Pages の上限 25MiB の内）。同じ大きさ・時刻のものは
  写し直さないので、10 分ごとの生成でも重くならない
- 利用条件は気象庁ホームページのコンテンツと同じ「公共データ利用規約（第1.0版）」。
  出典と、編集・加工したデータであることの記載を求める（NetCDF の属性にも入っている）
- `_headers`（サイト直下）で NetCDF の Content-Type と CORS を設定する

## 日別の観測データ（/data/daily/）

一番よく使われるデータ。`export_dist.py` が観測ストアの日別値（最高・最低・平均気温、
降水量、日照と品質・起時）を `dist/daily/` に、年ごと（全地点 × 1 年）と地点ごと
（1 地点 × 全期間）の NetCDF として書き出し、`generate.py` の `build_data_daily` が配る。

- 書き出しはストアが書き換わったとき（毎日の蓄積・月次の確定値置換の後）だけ走る。
  10 分ごとの生成では写すだけ（書き出しは 30 秒ほど）
- 生成時刻をファイルに入れず、中身が同じなら置き換えないので、Pages へ上がるのは
  変わった年・地点だけ
- 廃止地点（旧 WeatherCore のダンプにある阿蘇山・伊吹山など 56 地点）も含める
- 全期間・全地点を 1 本にしたものは Pages の 25MiB を超えるので作らない
  （年ごとは最大 1.5MB、地点ごとは最大 0.6MB）

## 旧サイトの URL との互換（weather.time-j.net）

公開先は `weather.time-j.net`。**いまは旧システム（WeatherCore）が動いていて、
新しいサイトはいずれ同じドメインでそれを置き換える。** 旧 URL で張られたリンクや
検索結果を切らないよう、旧 URL はすべて新しいサイトのどこかに着くようにする。
`check_legacy_urls.py` が、旧サイトをリンクでたどって集めた 4,154 件
（`legacy/old_paths.json`）を生成済みの `public/` でたどって確かめる（2026-10-06、
実データで 4,154 件すべて着く。Pages のプレビューでも同じ）。

### URL は小文字（正規化）

旧サイト（ASP.NET）は大文字小文字を区別しなかったので、リンクの表記が揺れている
（`/Stations/JP/Tokyo`・`/stations/jp/tokyo`）。Pages は区別し、`_redirects` の一致も
区別する。そこで **サイトの URL はすべて小文字にし、大文字を含む要求は小文字へ送る。**

- 生成側は、書き出すパスとページ内のサイト内リンク（`href`・`src`・`action`）を
  `weatherlib/siteurl.py` で小文字にする。テンプレートやコードには旧サイトと同じ表記
  （`/Summer/Ranking`）が残っていてよい。データファイルの名前も小文字
- 生成の最後に、`public/` から大文字を含むパスを消す（`sweep_uppercase`）。小文字に
  する前のページを残すと、同じページが 2 つの URL で公開されるため
- 大文字から小文字への転送は **Cloudflare の転送ルール**（time-j.net のゾーン。下）。
  ルールの無い環境（`*.pages.dev` など）では `404.html` のスクリプトが小文字へ送る
  （404 のあとの転送なので、検索エンジン向けには転送ルールが要る）

転送ルール（Rules → Redirect Rules → Single Redirects。設定は運用者が行う。
無料プランでは正規表現の `matches` が使えないので、大文字は `contains` を並べて見る）:

```
名前:   URL を小文字へ
条件（式を編集）:
  (http.host eq "weather.time-j.net" and (
    http.request.uri.path contains "A" or http.request.uri.path contains "B" or
    … 同じ形で "C" から "Y" まで …
    http.request.uri.path contains "Z"))
転送先: Dynamic   式: concat("https://weather.time-j.net", lower(http.request.uri.path))
状態:   301    クエリ文字列を保持: オン
```

### 転送（_redirects）

小文字にした後の URL について、同じページが無いものを `_redirects` で送る
（`generate.py` の `legacy_redirects`）。表は旧サイトの表記で書き、最後に小文字に
そろえる。行き先が自分自身になった行（旧サイトの別名 `abashiri` など）は除く。

- **地点ページ・雨温図は旧サイトの URL 名の小文字**（`/stations/jp/abashiri`）で作る。
  対応は `legacy_slugs.py` が旧サイトの一覧（府県番号 ＋ 日本語名）から取り、
  `WeatherStatic/legacy/station_slugs.json` に残した（910 地点）。旧サイトが自分の
  リンクで使っていた別名（`/Climate/Chart/akita`、`Tokyoold` など 61 件）もそこにある。
  **旧システムを止めたら取れないので、この JSON と `old_paths.json` は消さない**
- 301 … 同じ中身のページが別の URL にある（入口・平年値の月・予報図・廃止地点）
- 302 … 今年・今季の年のページ（`/summer/ranking/2026` → `/summer/ranking/`）
- 200 … 日ごと・月ごとの過去のページ（上の「過去の記録のページ」）
- Pages の `_redirects` は**実ファイルより先に効く**。ファイルのある URL に当たる行を
  置かないこと（過去の年の夏・冬のページなど）
- Pages の `_redirects` の決まり: 固定の転送をパターン付きより前に置く。名前付きの
  置き場所（`:year`）は転送先で使わないと無効になり、クエリには差し込まれない。
  なので `*` を使う。200 の行き先は拡張子なし（`/history/day/`。`day.html` と書くと
  Pages が拡張子を外す 308 を挟む）。上限は固定 2,000 件・パターン付き 100 件。
  生成時に並び順・小文字・自己当たり・上限を確かめる
- URL 名を変えたときに残る古いページは、生成のたびに片付ける（地点ページ・雨温図・
  実況の地点ページ・雨温図の比較用データ）
- テンプレートで固定の地点へリンクするときは `slug_by_intl(国際地点番号)` を使う。
  URL 名を直書きしない
- `sitemap.xml` の URL は `https://weather.time-j.net`（`WEATHER_SITE_ORIGIN` で変えられる）

## テンプレート

Jinja2。`templates/_layout.html` が全ページの外枠。

```
_layout.html          外枠（ヘッダ・フッタ・テーマ切替）
partials/_navbar.html グローバルナビ（8 項目。nav_active で現在地）
partials/_region_filter.html  地方別の絞り込みチップ
temperature/_submenu.html     気温ページのサブメニュー
status/_submenu.html          実況ページのサブメニュー
```

ナビは配列をループして出す。8 項目それぞれに条件式を手で書き写す形だと、
片方だけ足し忘れる（`class="active"` は付くのに `aria-current` が無い、など）。

## CSS

Bootstrap 3 と jQuery は使わない。2 枚だけ。

| | 役割 |
|---|---|
| `assets/site-base.css` | 土台。Bootstrap から借りていたクラス（`.container` `.row` `.col-xs-*` `.btn` `.navbar` 等）を自前で実装 |
| `assets/site.css` | 配色とコンポーネント。CSS 変数で light/dark を切り替える |

読み込み順は base → site。base が「Bootstrap が提供していた土台」、site が
見た目。この分担なので、既存テンプレートを書き換えずに Bootstrap を外せた。

### ヘッダは幅で 3 段階

メニュー 8 項目が約 735px を要するので、1 行に収まるのは container が
最大幅（1180px）のときだけ。

| 幅 | 形 |
|---|---|
| 1181px〜 | 1 行（ロゴ｜メニュー｜検索｜テーマ切替） |
| 961〜1180px | 2 段（1 段目にロゴと切替、2 段目にメニューと検索） |
| 〜960px | ハンバーガー |

中間幅で項目ごとに折り返すと「アプリだけ 3 行目」という崩れ方をするので、
2 段目は `flex-basis: 100%` で行ごと確保する。

開閉は checkbox ＋ CSS で行う（JS 不要）。ただし checkbox を
`display: none` にするとキーボードで到達できなくなるため、
「見えないがフォーカスは受ける」形にしてある。961px 以上では
ハンバーガー自体が無いので `display: none` に戻し、何も起きないタブ止まりを
作らない。

## ブラウザ側で動くもの

静的サイトだが、いくつかはブラウザで動く。

| | 何を |
|---|---|
| テーマ切替 | `<html data-theme>` を localStorage に保存。描画前に同期実行してちらつきを防ぐ |
| 地方別の絞り込み | 表の行を JS で出し入れ（`_region_filter.html`） |
| 雨温図の比較 | `assets/js/climate-compare.js` ＋ d3 |
| Python グラフ工房 | Pyodide でブラウザ内 Python。`weatherlib/svgchart.py` をページに同梱 |

`/status/lab/` は `/data/amedas-today-{要素（小文字）}.json` を fetch する。
要素ごとにファイルを分けてあるのは、まとめると数 MB になり初回表示が
重いから（1 ファイル gzip 後およそ 60KB）。

```
public/data/amedas-today-temp.json          気温
public/data/amedas-today-humidity.json      湿度
public/data/amedas-today-pressure.json      現地気圧
public/data/amedas-today-normalPressure.json 海面気圧
public/data/amedas-today-wind.json          風速
public/data/amedas-today-precipitation10m.json 10 分降水量
public/data/amedas-today-precipitation1h.json  1 時間降水量
public/data/amedas-today-sun10m.json        10 分日照
public/data/current.json                    主要都市の現在値
```

## グラフは SVG を自前で作る

`weatherlib/svgchart.py`。matplotlib も JS のグラフ部品も使わない。

- `intraday_svg` … 当日の 10 分値推移（時刻軸）
- `trend_svg` … 複数日の推移
- 目盛りの刻みは `_nice_step`（log10 で桁を出す）

文字列で SVG を組み立てるだけなので依存が無く、Pyodide でもそのまま動く。
同じコードをサーバー側の生成とブラウザ内 Python の両方で使える。

## 検索

ヘッダに Google カスタム検索の窓がある。**外部への唯一の依存**。
外すなら代わりが要る（3,132 ページのサイトを検索なしにはできない）。
`public/` を走査して `<title>` から索引 JSON を作り、ブラウザ内で絞り込む
案がある。1,286 地点の名前が引けるので Google CSE より使い勝手は良くなる
見込み。未着手。

## これから考えること

**実況ページ 1,293 枚を 10 分ごとに作り直している**（43MB を毎回 Cloudflare
へ送る）。地点ページ 1,287 枚は同じ器に別の地点を流し込んでいるだけなので、
1 枚の器が R2 を直接読む形にすれば、この山は消える。

地点ページ 1,287 枚は検索の入口にもなるが、サイトが自分専用なら
その論拠は弱い。R2 直読みを選びやすくなった。

- 実況の表・地点ページ … R2 直読み（10 分ごとの更新が要る部分）。
  アプリと同じデータを同じ URL で読むので、実装の検証も兼ねる
- 統計・ランキング・雨温図 … サーバー側で生成（日次で足りる）
