# Facebook などで共有したときの絵と説明（OGP）（設計）

2026-10-18。設計: Opus（Weather アプリ開発）、実装: Sonnet（Weather アプリ実装）。

## 経緯

利用者の依頼: 「Facebookにリンクが表示される時に絵が表示されるようにして」。
利用者は、Facebook ページ（https://www.facebook.com/weathertimejnet）で、サイトの復活を知らせる。

今のサイトには、OGP（`og:*`）も `<meta name="description">` も無い（`templates/_layout.html`。
本番のトップでも確認した）。そのため共有すると、絵も説明も出ない。

実装の前に `docs/web.md` と `templates/_layout.html` を読む。

## 1. 絵（1200×630 の JPEG、2 枚）

置き場所は `WeatherStatic/wwwroot/Images/Og/` で、ファイル名は小文字にする。
公開の URL は小文字になる（`/Images/App/…` は `/images/app/…` へ 301。本番で確認）。

| ファイル | 使うページ | 中身 |
|---|---|---|
| `site.jpg` | 既定（下の 2 つ以外すべて） | 左に文字、右に雨温図 |
| `app.jpg` | `/app/`・`/app/forecast/`・`/app/develop/` | アプリの画面の例と文字 |

### site.jpg

- 背景は白か、サイトの色（`assets/site.css` の `--accent` 系）の淡い色。
- 左の 6 割に文字を置く。
  - 「個人開発気象統計」（大きく）
  - 「アメダスの今日の気温・10分ごとの実況・暑さ寒さのランキング・雨温図」（中くらい）
  - 「weather.time-j.net」（小さく）
- 右の 4 割に、東京の雨温図（月平均気温の折れ線と、月降水量の棒）を置く。
  - 軸の目盛りと月の数字は小さく。凡例は不要。
  - 下に「東京（平年値 1991–2020）」と小さく書く。
  - 値は気象庁の平年値（1991–2020、東京）を使う。
    - 気温（℃）: 5.4, 6.1, 9.4, 14.3, 18.8, 21.9, 25.7, 26.9, 23.3, 18.0, 12.5, 7.7
    - 降水量（mm）: 59.7, 56.5, 116.0, 133.7, 139.7, 167.8, 156.2, 154.7, 224.9, 234.8, 96.3, 57.9
    - リポジトリか deb2 の平年値のデータで照合できるなら、照合して報告する。違えばデータのほうを使う。
  - 色は、サイトの雨温図（`weatherlib/svgchart.py` の `uonzu_svg`）に揃える。
- Facebook は小さく切り詰めて表示することがある。文字は端から 60px 以上離す。

### app.jpg

- `wwwroot/Images/App/forecast-map.jpg`（解析 T+0 の天気図の画面）を背景にする。
  1200×630 に収まるよう、拡大と切り抜きをする。
- 下に濃い半透明の帯を敷き、白い文字を置く。
  - 「AIseed Weather」
  - 「数値予報の天気図を、自分のパソコンで描く（ECMWF・無料）」
- 予報の画面は使わない（`APP_SHOTS` と同じく、解析と観測だけを写す方針）。

### 作り方

- 一度だけ使う道具として、`WeatherStatic/make_og_images.py` を作る。
  - matplotlib と PIL で描き、`../.venv/bin/python make_og_images.py` で 2 枚を作り直せるようにする。
  - 作った JPEG もコミットする（generate のたびには作らない）。
- 字体は Noto Sans CJK JP など、日本語の出るゴシックを使う。
  - 無ければ `fc-list :lang=ja` で探す。
  - 何を使ったかを、スクリプトの先頭のコメントに書く。
- JPEG の品質は 85 前後にして、1 枚 300KB 以内を目安にする。
- `generate.py` の `ASSET_PATHS` が `Images` をまるごと写すので、置けば公開される。確かめる。

## 2. ページの `<head>`

`templates/_layout.html` の `<head>` に、次を足す。

```html
<meta name="description" content="{% block description %}{{ default_description }}{% endblock %}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="個人開発気象統計">
<meta property="og:locale" content="ja_JP">
<meta property="og:title" content="{{ page_title }}">
<meta property="og:description" content="（上の description と同じ文）">
<meta property="og:image" content="https://weather.time-j.net/images/og/{% block og_image %}site{% endblock %}.jpg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="（絵の説明）">
<meta name="twitter:card" content="summary_large_image">
```

- 同じ文を 2 回書かないよう、Jinja の `{% set %}` か `self.description()` で使い回す。
- `page_title` が無いページの扱いは、今の `<title>` と同じにする。
- **description の文**:
  - 既定: 「全国のアメダスの今日の気温、10分ごとの実況、暑さ・寒さのランキング、各地の雨温図。1880年からの観測を、個人で集計しています。」
    - 「1880年から」は、サイトが実際にその範囲を載せているかを確かめてから入れる。違えば、その部分を外す。
  - `/app/`: 「数値予報の天気図を自分のパソコンで描くデスクトップアプリ AIseed Weather。ECMWF の10日先までの予報、気象庁のナウキャストとアメダス。無料・オープンソース。」
  - `/app/forecast/`: 「旧 GFS の予報図のページの後継。ECMWF の数値予報の天気図を、AIseed Weather で自分のパソコンに描く方法。」
  - ほかのページは、既定のままでよい。
    - 地点・雨温図・ランキングのように、ページ名を入れると良くなる所が見つかれば、報告だけする。広げるかは後で決める。
- `og:url` は入れない。Facebook は共有された URL を使う。各ページの正規の URL を出す仕組みが今は無いので、無理に作らない。
- `/app/` 系の 3 枚のテンプレートで、`og_image` を `app` に、`description` をそれぞれの文に上書きする。

## 3. 確かめ方

1. dev で生成（テストデータ）し、次を確かめる。
   - `public/index.html` と `public/App/index.html` などの `<head>` に、上のタグが入っている。
   - 文字の中の `"` や `&` が正しくエスケープされている。
2. 2 枚の絵を開いて目で見る。
   - 文字が切れていない。
   - 日本語の字体が出ている。
   - 1200×630 である。
   - 300KB 以内である。
3. 既存の点検（`check_legacy_urls.py` があれば dev で）と、WeatherStatic の試験が通る。
4. コミットと push は、利用者がそのセッションで指示したときだけ。
   公開（deb2 → Cloudflare）は Opus が、利用者の指示で行う。
5. 公開の後に、Facebook の「シェアデバッガー」（https://developers.facebook.com/tools/debug/）で
   URL を読み直させる。前に共有を試した URL は、古い表示が残っているため。
   これは利用者に案内する（Facebook へのログインが要るため）。
