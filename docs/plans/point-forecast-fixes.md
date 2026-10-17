# 地点予報の天気表示の修正と、画面の日本語化（設計）

2026-10-17。Windows 版（app-v0.1.0）を win11 で動かした画面から見つかった点。
設計: Opus、実装: Sonnet。

実装の前に読む skill（AGENTS.md の決まり）: aiseed-conventions, flet-declarative,
flet-component-basics, user-action-fetch, open-meteo-access。
読んだものを報告の最初に1行で書く。

対象は 3 つ。A → B → C の順に進める。

---

## A. 週間予報の「1日の天気」の決め方（ほぼ毎日「霧雨」になる）

### 原因

`components/point_forecast_view.py` の `_forecast_summary_strip` が、1日分の
1時間ごとの WMO 天気番号の **最大値** をその日の天気にしている
（`pl.col("weather_code").max()`）。WMO の番号は 0〜3 が晴れ〜曇り、51 以上が
降水なので、0.1 mm の霧雨が 1 時間あるだけで一日「霧雨」になる。
`_WEATHER_BY_CODE` の上のコメント（「worst of the day wins」）も、この考えを書いている。

### 新しい決め方

気象庁の日々の天気（日中 06〜18 時を主に見る。雨の日は日降水量 1 mm 以上）に寄せる。
次の純粋な関数を新しく作る。Flet を import しないので、services に置ける。

`src/aiseed_weather/services/weather_summary.py`

```python
def daily_weather_code(
    local_hours: Sequence[int],          # 時刻（-1h ずらした後の、その地点の時 0..23）
    codes: Sequence[int | None],         # WMO weather_code（1時間ごと）
    precip_mm: Sequence[float | None],   # 1時間降水量
) -> int | None:
```

規則（上から順に、最初に当てはまったものを返す）:

1. code が None でない行が無い → `None`。
2. **日中** = `6 <= local_hour < 18` の行。日中の行が無い（今日の夕方以降など、
   その日の一部しか無い）ときは、ある行すべてを日中とみなす。
3. **雷**: 日中に 95 以上の code があれば、その中の最大（95 / 96 / 99）。
4. **雨や雪の日**: その日（24時間全部）の降水量の合計が **1.0 mm 以上**のとき。
   24時間全部の降水コード（51〜67, 71〜77, 80〜86）を次の4つに分ける。
   - 霧雨 = 51, 53, 55
   - 着氷性 = 56, 57, 66, 67
   - 雨 = 61, 63, 65, 80, 81, 82
   - 雪 = 71, 73, 75, 77, 85, 86

   いちばん時間数の多い組を選ぶ。同数なら 雪 > 雨 > 着氷性 > 霧雨 の順で選ぶ。
   選んだ組の中で最大の code を返す。降水コードが 1 つも無いのに合計が
   1 mm 以上のとき（モデルの不整合）は、61 を返す。
5. **それ以外**（降水が無いか、1 mm 未満）: 日中の空の様子で決める。日中の
   各 code を空の段に直す: 0→0、1→1、2→2、3→3、45/48→霧、それ以外の降水コード→3。
   - 霧が日中の行の半分以上 → 45
   - そうでなければ、霧を除いた段の **中央値**（偶数個なら小さいほう、`sorted(x)[(n-1)//2]`）
     → 0 / 1 / 2 / 3

1 mm 未満の降水は天気には出さないが、カードの降水量（例 0.1 mm）はそのまま出す。

### 画面側

`_forecast_summary_strip` の `group_by("date").agg(...)` の
`pl.col("weather_code").max()` をやめる。代わりに、その日の `local_hour` と
`weather_code`、`precipitation` を list で集めて、`daily_weather_code` に渡す。
`local_hour` は -1h ずらした後の時。
降水量の合計 `p_sum` と最高・最低は今のまま。

`_WEATHER_BY_CODE` の上の「max code が代表」というコメントを、新しい規則
（`daily_weather_code` を見よ）に書き直す。

### 試験 `tests/test_weather_summary.py`

- 日中ずっと 0 で、夜に 51 が 1 時間・0.1 mm → 晴れ側（0）。
- 日中 2,2,3,3,3 … が多い → 中央値で 3 になる例と 2 になる例。
- 合計 5 mm で、61 が 4 時間、71 が 1 時間 → 61 系の最大（61/63 のどちらか、例で決める）。
- 日中に 95 が 1 時間 → 95。
- 霧 45 が日中の半分以上 → 45。
- 18 時以降の 3 行だけ（日中なし）→ その 3 行で判定。
- 全部 None → None。

---

## B. 時間別予報: 夜は月の絵にする / 「晴時々曇」の絵を作り直す

### 原因

- 昼か夜かを見ていない。18 時〜翌 5 時も、晴れなら太陽の絵 `WB_SUNNY` を出す。
- code 2（晴時々曇）は `WB_CLOUDY` を黄土色 `#c8a14e` で描いている。`WB_CLOUDY` は
  雲だけの絵なので、「茶色い曇り」に見える（スクリーンショットの 03 時）。

### 昼夜の判定（取得は増やさない）

Open-Meteo の `is_day` は取らない。`HOURLY_VARS` を変えると、forecast・archive・
ensemble の 3 つの要求とキャッシュの鍵が変わるうえ、ensemble が対応しているか
分からないため。太陽高度を手元で計算する。

`src/aiseed_weather/services/solar.py`（純粋な関数。Flet・numpy なしで書ける）

```python
def solar_elevation_deg(when_utc: datetime, lat: float, lon: float) -> float
def is_daylight(when_utc: datetime, lat: float, lon: float) -> bool:
    return solar_elevation_deg(when_utc, lat, lon) > -0.833   # 日の出・日の入りの定義
```

- NOAA の簡略式（赤緯と均時差、通日からの近似）で足りる（誤差は数分）。
- `when_utc` は tz 付きの datetime（naive なら ValueError）。

試験 `tests/test_solar.py`:

- 東京（35.69, 139.69）2026-10-17 12:00 JST → 昼、00:00 JST → 夜。
- 日の出（約 05:50 JST）の前後 20 分で判定が変わる。
- 赤道の春分の正午に高度が約 90°（±2°）。
- 北極点の 6 月は終日昼、12 月は終日夜。

### 絵を一か所で作る

`point_forecast_view.py` に `_weather_icon(code, *, size: int, night: bool = False) -> ft.Control`
を作る。`_forecast_summary_strip`、`_hourly_forecast_strip`、`_jma_forecast_card` の
`ft.Icon(icon, color=color, size=...)` を、これに置き換える。

| code | 昼 | 夜 |
|---|---|---|
| 0, 1 | `WB_SUNNY` #f5a623（今のまま） | `NIGHTLIGHT` #6c7a96 |
| 2 | 太陽と雲を重ねる（下記） | `NIGHTS_STAY` #6c7a96 |
| 3 以降 | 今のまま（`_WEATHER_BY_CODE`） | 昼と同じ |

code 2（昼）の重ね方: 大きさ `size` の `ft.Stack` に次の 2 つを置く。

- `WB_SUNNY`（#f5a623、size×0.7、left=0, top=0）
- `CLOUD`（#9aa5b1、size×0.75、right=0, bottom=0）

位置の引数（left/top/right/bottom）は Flet 1.0.4 の LayoutControl にある
（`mcp__flet__get_api` で確かめた）。

`_WEATHER_BY_CODE` の 2 の行は、色を雲の灰色 `#9aa5b1` にする（文字や
凡例に色を使う所があれば、それに合わせるため）。

ラベル（「晴れ」など）は昼夜で変えない。気象庁も夜を「晴れ」と書く。

時間別のセルでは `night = not is_daylight(row["timestamp"], location.latitude, location.longitude)`
で判定する。`timestamp` は UTC の tz 付き。週間予報と気象庁の週間カードは
`night=False`。

### 合わせて直す

`_jma_forecast_card` は、気象庁の天気コード（100, 101, 200, 300 …）を、WMO 用の
`_weather_descr` にそのまま渡している。そのため「code 101」と出るはず
（`services/jma_forecast_service.py` は `weatherCodes` を変換せずに渡している）。
→ 画面で確かめてから直す。直すなら、気象庁コードの 100 の位で次のように
WMO の代表に写す小さな関数を `weather_summary.py` に置き、試験も足す。

- 1xx → 1
- 2xx → 3
- 3xx → 63
- 4xx → 73

正確な文はカードに `weather_text` が別にあるので、絵の大分類が合えばよい。
これは、利用者にまだ確認していない。実装はするが、報告で別項目として目立たせる。

---

## C. 画面の文字を日本語だけにする

### 方針

- 「日本語 / English」の併記は、英語の側を消して日本語だけにする（約 120 か所）。
- 英語だけの見出し・ボタン・tooltip・hint・空の状態の文（例 "Coming soon"）も日本語にする。
- **そのまま残すもの**:
  - データ源・機関・製品の固有名（ECMWF, ERA5, IFS, AIFS, GFS, JMA, AMeDAS, Open-Meteo）
  - 単位、変数の略号（MSL, hPa, gpm, dam, UTC, JST）
  - アプリ名「AIseed Weather」
  - 帰属・ライセンスの文（"Data: Open-Meteo … CC-BY-4.0" など。文言は規約で決まっているため）
  - 例外の型名やメッセージをそのまま出している所（`f"{type(exc).__name__}: {exc}"`）
  - ログ（`logger.*`）、コード内のコメント・docstring、変数名
- 書き出す図（`figures/point_forecast_chart.py`, `figures/canvas_timeseries.py`,
  `figures/regions.py` の名前）も日本語だけにする。図に日本語の字体が出ることを
  PNG を作って確かめる。

### 主な所

`grep -rnP '[\p{Han}\p{Hiragana}\p{Katakana}][^"\n]* / [A-Za-z]' src/aiseed_weather` で拾える。

| ファイル | 数 |
|---|---|
| components/map_view.py | 53 |
| products/catalog.py | 15 |
| figures/regions.py | 14 |
| components/app.py | 13 |
| components/point_forecast_view.py | 9 |
| figures/point_forecast_chart.py, figures/canvas_timeseries.py | 各 5 |
| components/observation_common.py | 3 |
| components/radar_view.py | 2 |
| components/observation_view.py | 1 |

`" / "` が「A / B の選択」という意味で使われている所は残す
（例 `"JMA / AMeDAS"`、`"タイムゾーン / JMA / AMeDAS"`）。機械的に消さず、1 つずつ判断する。
英語だけの文字は grep で拾い切れない。`ft.Text("…")`、`label=`、`tooltip=`、
`hint_text=`、`title=`、`text=` に英字で始まる文字列を渡している所を探す。

例:

- `"地点 / Points"` → `"地点"`
- `"取得 / Fetch"` → `"取得"`
- `"ターミナル / Terminal  (Coming soon)"` → `"ターミナル（準備中）"`
- `"緯度 / Latitude (-90..90)"` → `"緯度（-90〜90）"`
- `"高度 / GH"` → `"高度"`

### 文書

skill と docs の中で、画面の文字を引用している所（`取得 / Fetch` など。
`.agents/skills/user-action-fetch/SKILL.md`、`.agents/skills/data-flow/SKILL.md`、
`docs/` を grep）を新しい文字に合わせる。
`aiseed-conventions` の「Units and conventions」の近くに、次の1行を足す。

> 画面と図の文字は日本語。固有名・単位・略号・帰属文はそのまま。

---

## 確かめ方（全部終わってから）

1. `./.venv/bin/python -m pytest -q`（`tests/test_flet_api_names.py` を含む）。
   これまでの失敗は無い。
2. 画面で確かめる。`./.venv/bin/python -m aiseed_weather.main` で立ち上げ、
   地点予報の「概要」を見る。
   - 週間予報が「霧雨」だらけでない
   - 夜の時間が月の絵
   - 03 時のような code 2 が太陽＋雲の絵
   - タブ・見出しが日本語だけ

   画面を撮れない場合は、その旨を報告する（推測で「確かめた」と書かない）。
3. コミットも push もしない（利用者が頼んだときだけ）。変えたファイルを A / B / C ごとに報告する。

---

## D. 気象庁の週間カード: 気象庁の天気コード表をそのまま使う（2026-10-17 追加）

### 経緯

B で入れた `wmo_code_from_jma` は、100 の位だけを見て晴・曇・雨・雪の 4 つに写している。
そのため 201（曇時々晴）と 202（曇一時雨）が、どちらも「曇り」と出る（徳島の実データで確認）。
利用者の判断（2026-10-17）: 気象庁のコード表で進める。

### 表のもと

気象庁の天気予報ページ（https://www.jma.go.jp/bosai/forecast/）に埋め込まれた
`TELOPS` の表を使う。中身は `コード: [昼の絵.svg, 夜の絵.svg, 大分類, 日本語, 英語]` で、118 種ある。
2026-10-17 に取り出した「コード・昼の絵の番号・日本語」を、この文書の末尾に付けた。
実装はこの付表から作る（取得し直さない）。

昼の絵の番号は 30 種ある。気象庁は、同じ絵を複数のコードで使い回している
（例: 103 晴時々雨 も 102 の絵）。

### 新しいモジュール `src/aiseed_weather/services/jma_weather_codes.py`

Flet を import しない。

```python
@dataclass(frozen=True)
class JmaWeather:
    text: str                 # 気象庁の言葉そのまま（「曇時々晴」）
    main: str                 # "sun" | "cloud" | "rain" | "snow"
    sub: str | None           # 2 つ目の天気。無ければ None
    relation: str | None      # "時々"（同じ日に混じる）| "後"（移り変わる）| None

def jma_weather(code: int | None) -> JmaWeather | None
```

- `_TELOPS: dict[int, tuple[int, str]]` は、コード → (昼の絵の番号, 日本語)。付表の 118 行をそのまま持つ。
  先頭のコメントに、出典の URL と取り出した日付を書く。
- `_ICON_PARTS: dict[int, tuple[str, str | None, str | None]]` は、昼の絵の番号 → (main, sub, relation)。
  30 種を次のとおりとする。

| 絵 | main | sub | relation | | 絵 | main | sub | relation |
|---|---|---|---|---|---|---|---|---|
| 100 | sun | – | – | | 300 | rain | – | – |
| 101 | sun | cloud | 時々 | | 301 | rain | sun | 時々 |
| 102 | sun | rain | 時々 | | 302 | rain | cloud | 時々 |
| 104 | sun | snow | 時々 | | 303 | rain | snow | 時々 |
| 110 | sun | cloud | 後 | | 308 | rain | – | – |
| 112 | sun | rain | 後 | | 311 | rain | sun | 後 |
| 115 | sun | snow | 後 | | 313 | rain | cloud | 後 |
| 200 | cloud | – | – | | 314 | rain | snow | 後 |
| 201 | cloud | sun | 時々 | | 400 | snow | – | – |
| 202 | cloud | rain | 時々 | | 401 | snow | sun | 時々 |
| 204 | cloud | snow | 時々 | | 402 | snow | cloud | 時々 |
| 210 | cloud | sun | 後 | | 403 | snow | rain | 時々 |
| 212 | cloud | rain | 後 | | 406 | snow | – | – |
| 215 | cloud | snow | 後 | | 411 | snow | sun | 後 |
| | | | | | 413 | snow | cloud | 後 |
| | | | | | 414 | snow | rain | 後 |

- 表に無いコード（気象庁が新しく足した場合など）は、100 の位で main を決める
  （1 → sun、2 → cloud、3 → rain、4 → snow、sub と relation は None）。
  text は `f"コード {code}"`。`logger.info` で一度知らせる（同じコードで何度も出さない）。
  100 の位も 1〜4 でなければ None を返す。
- B の `weather_summary.wmo_code_from_jma` とその試験は消す（`_jma_forecast_card` だけが使っていた）。

### 絵 `point_forecast_view.py`

`_jma_weather_icon(w: JmaWeather | None, *, size: int) -> ft.Control` を作る。

- 部品の絵と色は、WMO 側（`_weather_icon`）と揃える。
  - sun: `WB_SUNNY` #f5a623
  - cloud: `CLOUD` #9aa5b1
  - rain: `UMBRELLA` #3a78b3
  - snow: `AC_UNIT` #8aa9c6
- sub が無いときは、main を `size` で 1 つ描く。
- relation が `"時々"` のときは、`ft.Stack`（width=height=size）に重ねる。
  - main: size×0.75、left=0, top=0
  - sub: size×0.55、right=0, bottom=0
  - 晴時々曇（101）は、WMO 側の code 2 とほぼ同じ見た目になる。
- relation が `"後"` のときは、`ft.Stack`（width=height=size）に並べる。
  - main: size×0.55、left=0, top=0
  - sub: size×0.55、right=0, bottom=0
  - 中央に `ft.Icons.CHEVRON_RIGHT`（GREY, size×0.35）を置き、移り変わりを示す。
  - Stack の中で中央に置く方法は、Flet 1.0.4 の API で確かめる（`alignment` など）。
- `w is None` のときは、従来どおり `HELP_OUTLINE`（GREY）。

### カード `_jma_forecast_card`

- 週間の各カードの絵は `_jma_weather_icon(jma_weather(day.weather_code), size=28)`。
- 文字は `JmaWeather.text`（「曇時々晴」）。`None` のときは「—」。
  - 「晴後一時雨か雷雨」のような長い語がある。カードの幅は 72 なので、
    `ft.Text(text, size=10, max_lines=2, text_align=CENTER)` にする。
  - カードの高さがそろうよう、2 行分の高さの入れ物（height は実測で決める）に入れる。
- 今日・明日・明後日の行（`short_rows`、気象庁の長い文 `weather_text` を出している所）にも、
  左に `_jma_weather_icon(..., size=20)` を足す。`short_term` の `weather_code` を使う。
  文はそのまま。

### 試験 `tests/test_jma_weather_codes.py`

- 付表の行数が 118 で、`_TELOPS` のすべての絵の番号が `_ICON_PARTS` にある。
- 次のコードの結果（text, main, sub, relation）:

  | コード | text | main | sub | relation |
  |---|---|---|---|---|
  | 100 | 晴 | sun | None | None |
  | 201 | 曇時々晴 | cloud | sun | 時々 |
  | 202 | 曇一時雨 | cloud | rain | 時々 |
  | 212 | 曇後一時雨 | cloud | rain | 後 |
  | 311 | 雨後晴 | rain | sun | 後 |
  | 209 | 霧 | cloud | None | None |

  209（霧）は、気象庁も 200 の絵を使っている。
- 表に無い 299 → main=cloud、text="コード 299"。
- 999 → None。None → None。

### 確かめ方

- 試験（`PYTHONPATH=src ./.venv/bin/python -m pytest -q tests`）。
- 画面（Web 版で可）で、徳島の気象庁週間カードを見る。
  - 201 は曇＋小さな太陽、202 は曇＋小さな傘。
  - 文字は気象庁の言葉。
  - カードの高さがそろっている。

### 付表: 気象庁 TELOPS（2026-10-17 取得）

コード、昼の絵、日本語の順（タブ区切り）。

```
100	100	晴
101	101	晴時々曇
102	102	晴一時雨
103	102	晴時々雨
104	104	晴一時雪
105	104	晴時々雪
106	102	晴一時雨か雪
107	102	晴時々雨か雪
108	102	晴一時雨か雷雨
110	110	晴後時々曇
111	110	晴後曇
112	112	晴後一時雨
113	112	晴後時々雨
114	112	晴後雨
115	115	晴後一時雪
116	115	晴後時々雪
117	115	晴後雪
118	112	晴後雨か雪
119	112	晴後雨か雷雨
120	102	晴朝夕一時雨
121	102	晴朝の内一時雨
122	112	晴夕方一時雨
123	100	晴山沿い雷雨
124	100	晴山沿い雪
125	112	晴午後は雷雨
126	112	晴昼頃から雨
127	112	晴夕方から雨
128	112	晴夜は雨
130	100	朝の内霧後晴
131	100	晴明け方霧
132	101	晴朝夕曇
140	102	晴時々雨で雷を伴う
160	104	晴一時雪か雨
170	104	晴時々雪か雨
181	115	晴後雪か雨
200	200	曇
201	201	曇時々晴
202	202	曇一時雨
203	202	曇時々雨
204	204	曇一時雪
205	204	曇時々雪
206	202	曇一時雨か雪
207	202	曇時々雨か雪
208	202	曇一時雨か雷雨
209	200	霧
210	210	曇後時々晴
211	210	曇後晴
212	212	曇後一時雨
213	212	曇後時々雨
214	212	曇後雨
215	215	曇後一時雪
216	215	曇後時々雪
217	215	曇後雪
218	212	曇後雨か雪
219	212	曇後雨か雷雨
220	202	曇朝夕一時雨
221	202	曇朝の内一時雨
222	212	曇夕方一時雨
223	201	曇日中時々晴
224	212	曇昼頃から雨
225	212	曇夕方から雨
226	212	曇夜は雨
228	215	曇昼頃から雪
229	215	曇夕方から雪
230	215	曇夜は雪
231	200	曇海上海岸は霧か霧雨
240	202	曇時々雨で雷を伴う
250	204	曇時々雪で雷を伴う
260	204	曇一時雪か雨
270	204	曇時々雪か雨
281	215	曇後雪か雨
300	300	雨
301	301	雨時々晴
302	302	雨時々止む
303	303	雨時々雪
304	300	雨か雪
306	300	大雨
308	308	雨で暴風を伴う
309	303	雨一時雪
311	311	雨後晴
313	313	雨後曇
314	314	雨後時々雪
315	314	雨後雪
316	311	雨か雪後晴
317	313	雨か雪後曇
320	311	朝の内雨後晴
321	313	朝の内雨後曇
322	303	雨朝晩一時雪
323	311	雨昼頃から晴
324	311	雨夕方から晴
325	311	雨夜は晴
326	314	雨夕方から雪
327	314	雨夜は雪
328	300	雨一時強く降る
329	300	雨一時みぞれ
340	400	雪か雨
350	300	雨で雷を伴う
361	411	雪か雨後晴
371	413	雪か雨後曇
400	400	雪
401	401	雪時々晴
402	402	雪時々止む
403	403	雪時々雨
405	400	大雪
406	406	風雪強い
407	406	暴風雪
409	403	雪一時雨
411	411	雪後晴
413	413	雪後曇
414	414	雪後雨
420	411	朝の内雪後晴
421	413	朝の内雪後曇
422	414	雪昼頃から雨
423	414	雪夕方から雨
425	400	雪一時強く降る
426	400	雪後みぞれ
427	400	雪一時みぞれ
450	400	雪で雷を伴う
```

---

## E. 1 mm 未満の降水の扱い（2026-10-18 追加）

### 経緯

利用者より: 「日本の場合は、数値予報では1 mm 未満の降水がよく現れます。」

徳島の実データ（10-17 取得）でもそうなっている。
- 10/19 は 1.6 mm が 11 時間に散らばり、すべて霧雨コード（51〜55）。
- 10/20 は 0.7 mm が 6 時間に散らばっている。

A の規則（1日の合計が 1 mm 以上なら雨の日）では、10/19 は「霧雨」になる。
時間別では、0.1 mm の時間ごとに霧雨の絵が並ぶ。
モデルのこの弱い降水は、多くの場合、実際には観測されない。AMeDAS の雨量計は 0.5 mm 単位なので、0.5 mm 未満は「降水なし」と記録される。
気象庁の予報文も「霧雨」をほとんど使わない。

### 規則（`services/weather_summary.py`）

基準を **1 時間 0.5 mm**（AMeDAS の最小単位）に置き、時間別と週間を同じ基準でそろえる。

```python
WET_HOUR_MM = 0.5

def hourly_display_code(code: int | None, precip_mm: float | None) -> int | None:
    """降水コードなのに、その時間の降水が 0.5 mm 未満（または欠測）なら 3（曇り）にする。
    雷（95 以上）、霧（45/48）、降水以外のコードはそのまま返す。"""
```

`daily_weather_code` は次のように変える。

- 手順 4「雨や雪の日」の判定:
  - 今まで: 24 時間の降水量の合計が 1.0 mm 以上。
  - 変更後: **1 時間 0.5 mm 以上の時間だけ**を足した合計が 1.0 mm 以上。
- 雨か雪かの組を数えるときも、0.5 mm 以上の時間のコードだけを使う。
- 霧雨の組をやめて、雨の組に入れる。
  - 雨の組の中で最大のコードを返す。
  - 雨の組が霧雨コード（51/53/55）だけなら、61（小雨）を返す。
  - 1 時間 0.5 mm 以上も降る霧雨は、霧雨とは言いにくいため。
- 手順 5（空の様子）で降水コードを 3 に読み替えている所は、そのまま。
- 雷（手順 3）は、降水量に関係なく今のまま。
- 合計の 1 mm 以上は、0.5 mm 以上の時間だけで足すので、降水コードが無いのに満たす場合が今より減る。
  そのときは、今と同じく 61 を返す。

### 画面（`point_forecast_view.py`）

- 時間別のセル:
  - 絵は `_weather_icon(hourly_display_code(wcode, precip), size=22, night=night)` で描く。
    夜の判定は今のまま。曇りに読み替えた時間は、夜でも曇りの絵になる。
  - 降水量の数字は、0.5 mm 以上のときだけ出す（今は 0.05 mm より多いとき）。
- 週間のカード:
  - 降水量の表示は、合計が 0.5 mm 以上のときだけ `"{:.1f} mm"` で出す。それより少なければ「—」。
  - 合計は今のまま、すべての時間を足す。天気の判定に使う「0.5 mm 以上の時間だけの合計」とは別物。
  - 色の切り替え（`> 0.05`）も 0.5 に合わせる。
- 「変数」を選んで出すグラフ、TSV、日次サマリの表は、変えない（生の値を見る所なので）。

### 試験（`tests/test_weather_summary.py` を書き換え・追加）

- 霧雨 51 の時間が 11 時間、各 0.1〜0.2 mm（合計 1.6 mm）、日中は 1〜2 → 雨にしない（空の段の中央値）。
- 61 の時間が 2 時間、各 0.6 mm（合計 1.2 mm）→ 61。
- 霧雨 53 の時間が 3 時間、各 0.5 mm（合計 1.5 mm）→ 61（霧雨の組が無いので）。
- 雨 63 が 2 時間（各 1.0 mm）と、雪 71 が 1 時間（0.6 mm）→ 63。
- `hourly_display_code`:
  - (51, 0.1) → 3
  - (61, 0.5) → 61
  - (61, None) → 3
  - (95, 0.0) → 95
  - (45, 0.0) → 45
  - (1, 0.0) → 1
  - (None, 1.0) → None
- A の試験のうち、「合計 1 mm 以上で降水コードが無い → 61」は、0.5 mm 以上の時間の合計で 1 mm を満たすデータに直す。
  ほかの A の試験も、新しい規則で意図が変わるものは直し、どれを直したかを報告する。

### 確かめ方

- 試験。
- 画面（Web 版で可）で、徳島の時間別と週間を見る。
  - 0.5 mm 未満の時間は曇りの絵で、数字は出ていない。
  - 週間の 10/19 のような「弱い降水が散らばる日」は、霧雨でなく空の様子で出る。
