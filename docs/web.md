# サイトの構成（個人開発気象統計）

3,132 ページ・112MB の静的サイト。Cloudflare Pages に置く。
運用手順は [operations.md](operations.md)。

## 誰のためのサイトか

**恐らく自分専用。** 普通の人にはデスクトップ版（AIseed Weather）を勧める
（`/App/` がその案内）。サイトを公開したままにしておくのは、

- 「気象庁の観測をどこまで個人が扱えるか」の実証であること
- 裏で動く収集の仕組みが、**アプリの読む R2 のデータも作っている**こと
  （サイトを止めてもデータ基盤は止められない）

の 2 つによる。閲覧者の多寡は設計の前提にしない。

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

| URL | 数 | 内容 | 作る人 | 更新 |
|-----|----|------|--------|------|
| `/` | 1 | 主要都市の現況・30 日推移・雨温図 | generate | 毎時 |
| `/Temperature/…` | 4 | 今日の最高・最低（主要都市／各地） | generate | 毎時 |
| `/Summer/…` `/Winter/…` | 8 | 暑さ・寒さのランキングと日数一覧 | generate | 毎時 |
| `/Climate/Chart/{地点}` | 896 | 雨温図（平年値の気温と降水量） | generate | 毎時 |
| `/Stations/JP/{地点}` | 896 | 地点別の気候値・30 日推移・平年値 | generate | 毎時 |
| `/Monthly/…` | 26 | 月別気温（観測値・平年値） | generate | 毎時 |
| `/Precipitation/` | 1 | 降水量ランキング | generate | 毎時 |
| `/Status/Temperature/` `/Wind/` `/Precipitation/` `/Snow/` | 4 | 実況（10 分値） | generate_status | 10 分 |
| `/Status/Station/{地点}` | 1,287 | 地点別の 10 分値 | generate_status | 10 分 |
| `/Status/Records/` | 1 | 観測史上 1 位の更新状況 | generate_status | 10 分 |
| `/Status/Lab/` | 1 | Python グラフ工房（ブラウザ内 Python） | generate_status | 10 分 |
| `/About/` `/App/` | 2 | サイト説明・デスクトップ版の案内 | generate | 毎時 |

**10 分ごとに作り直すのは Status 配下の 1,293 ページ**（43MB）。
残りは毎時。`generate.py` はサイト全体を、`generate_status.py` は Status
だけを受け持つ。分けているのは、10 分ごとに 3,132 ページを作り直すのが
無駄だから。

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

`/Status/Lab/` は `/data/amedas-today-{要素}.json` を fetch する。
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
