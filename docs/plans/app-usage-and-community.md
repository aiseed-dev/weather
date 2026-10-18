# アプリの利用台数の記録と、Facebook・Discord へのつながり（設計）

2026-10-18。設計: Opus（Weather アプリ開発）、実装: Sonnet（Weather アプリ実装）。

## 経緯と利用者の決定

- Search Console（3 か月）では、検索から来た人の大半が旧サイトの GFS・NOAA の天気図を探していた。
  利用者は、アプリを普及させたいと考えている。
- 利用者の依頼:
  - 「アプリにも、使っている人がわかる程度の analytics が入れられないか」
  - 「discord 及び Facebook のページとリンクさせられないか」
- 利用者の決定（2026-10-18）:
  - 利用の記録は **既定で送る**。config.toml で止められる。
  - Facebook のページはある（URL はまだ受け取っていない）。Discord はまだ無い。

実装の前に読む skill: aiseed-conventions, flet-declarative, flet-component-basics,
first-run-setup, user-action-fetch。読んだものを報告の最初に1行で書く。

---

## F. 利用の記録（usage report）

### 何を送るか（これだけ）

送り先は、サイトと同じ自前の解析 kaiseki の受け口にある、アプリ用の入口
`POST https://analytics.aiseed.dev/v1/app`。受け口は deb2 にあり、アクセスログを取らず、IP を残さない。

この入口は、2026-10-18 に「aiai-toolsのclaude」が提案した。作るのは、発注者（利用者）の確認の後になる。
既存の `/v1/hit` を使わない理由は次のとおり。
- `/v1/hit` は、`KAISEKI_SITES` に載ったサイトなら Origin を見ない。
- アプリのためにサイトを載せると、Web のページの記録も、どこからでも偽って送れるようになる。

`/v1/app` は、`KAISEKI_APPS` に載ったサイトだけを Origin なしで受ける。受けるのは次の 4 項目だけ。

```json
{"site": "weather.time-j.net", "version": "0.1.1", "os": "windows", "channel": "release"}
```

- `os`: `windows` / `macos` / `linux`
- `channel`: `release`（配布版）/ `source`（手元の実行）
- **送らないもの**: ua、title、sid、vid（人や訪問を見分ける ID）、地点、設定の中身、
  画面の大きさ、言語、タイムゾーン、何を見たか。
- HTTP の User-Agent は `AIseed Weather/0.1.1 (Windows; release)` の形で正直に名乗る
  （`jma_endpoints.USER_AGENT` の形に揃え、版を入れる）。受け口は、これを記録しない。
- 入口ができるまでは 404 などで断られる。アプリは黙って次の起動に回すだけなので、
  先に出してかまわない。
- アプリには秘密の鍵が無いので、偽った記録で数を増やされることは防げない。
  数は「多めにもなりうる目安」と読む。

日ごとの `app_start` の件数が、その日に使われた台数（1 台 1 日 1 件）になる。
人を見分けないので、「何人」ではなく「何台・何日」までしか分からない。それで足りる。

### いつ送るか

- 起動して画面が出たあと、**その日（その PC の日付）にまだ送っていなければ 1 回**。
- 画面の表示を待たせない。
  - 背景のタスク（`page.run_task`）で送り、時間切れは 5 秒にする。
  - 失敗（ネットが無い、受け口が断る）しても、利用者には何も出さない（`logger.info` だけ）。
- 送れた日（2xx の応答）だけを記録する。失敗した日は、次に起動したときにまた試す。
- 記録の置き場所: `~/.config/aiseed-weather/usage.json`（`{"last_sent": "2026-10-18"}` だけ）。
  config.toml と同じフォルダー。

### 「データの取得は利用者の操作のときだけ」との関係

これは、データの **取得** ではなく、1 日 1 回の小さな **送信** で、受け取るものは無い。
`aiseed-conventions` の Forbidden にある「起動時の自動取得」には当たらない。
ただし紛らわしいので、例外として skill に明記する（下の「文書」）。

### 止め方（config.toml）

`models/user_settings.py`:

- `UserSettings` に `usage_report: bool` を足す。
- config.toml に鍵が無いときは `True`（利用者の決定「既定で送る」）。
- `true` / `false` 以外の値は、ほかの鍵と同じく設定の誤りとして扱う。
  エラーの文は日本語で、ConfigStatusPanel に出る。
- テンプレート `_TEMPLATE` の最後に、次を足す。
  テンプレートの他の部分は英語だが、ここは利用者に向けた知らせなので日本語にする。

```toml
# ---- 利用の記録 ----
# 起動した日に 1 回だけ、アプリの版と OS（Windows など）を作者の解析（analytics.aiseed.dev）へ送ります。
# 人や PC を見分ける情報・地点・設定・見たものは送りません。使われている台数を知るためです。
# 送りたくなければ false にしてください。くわしくは https://weather.time-j.net/kaiseki/
usage_report = true
```

### 既存の利用者への知らせ（2026-10-18 追加）

すでに config.toml を持つ人は、`usage_report` の鍵を持っていない。初回の案内も見ないので、
黙って送り始めることになる。それはしない。外部送信の知らせを、送る前に見せる。

- 鍵が無い人には、その起動で一度だけ知らせを出す。
  - 閉じられる帯か SnackBar で出す。
  - 文は初回の案内と同じ趣旨にし、「設定ファイルを開く」ボタンを付ける。
- 知らせを出したことを `usage.json` に記録する（`"notice_shown": "YYYY-MM-DD"`）。
- その起動では送らず、次の起動から送る。
- 次の人には出さない。
  - 鍵を書いてある人（true でも false でも）
  - 新しく設定ファイルを作った人（初回の案内で知らせ済み）
- 試験: 鍵が無い人の最初の起動では、送らずに知らせを記録する。2 回目の起動で送る。

### 版と種別（2026-10-18 改め: 版はタグだけで決める）

利用者より: 「他のアプリはもっと簡単にできています」。
そこで、版の番号をファイルに書いて手で上げるのをやめる。**リリースは、タグを打って push するだけ**にする。

```bash
git tag app-v0.1.1 && git push origin app-v0.1.1
```

- 正本は **タグ**（`app-vX.Y.Z`）。リポジトリのどのファイルにも、配布版の版を書かない。
- GitHub Actions（`windows-app.yml`）の動き:
  - タグのとき: `GITHUB_REF_NAME` から `X.Y.Z` を取る。`^\d+\.\d+\.\d+$` でなければ失敗させる。
    `src/aiseed_weather/_build_version.py` に `__version__ = "X.Y.Z"` を書く。
    `flet build windows --build-version X.Y.Z` で組み立てる。
  - タグでないとき（main への push、手動）: `_build_version.py` に
    `__version__ = "0.0.0.dev0+<コミットの短い sha>"` を書く。`--build-version` は付けない。
- `_build_version.py` は `.gitignore` に入れる。
- `src/aiseed_weather/__init__.py`:

  ```python
  try:
      from aiseed_weather._build_version import __version__
  except ImportError:          # 手元の実行（組み立てていない）
      __version__ = "0.0.0.dev0"
  ```

- 根の `pyproject.toml` は `version = "0.0.0"`（固定）に戻し、hatch の dynamic をやめる。
  コメントに「配布版の版はタグで決まる。ここは上げない」と書く。
- `desktop/pyproject.toml` の version は、`--build-version` が上書きするので触らない。
  コメントに同じことを書く。
- `tools/check_version.py` とその試験、windows-app.yml の突き合わせの段は消す
  （版を書く所が無いので、食い違いが起きない）。
- 種別（channel）は今のまま。環境変数 `AISEED_WEATHER_BUILD` で決め、
  `desktop/src/main.py`（配布版の入口）が `aiseed_weather.main` を import する前に `release` を入れる。
  無ければ `source`。
- 手元の実行（source）の利用の記録には、版 `0.0.0.dev0` が入る。それで区別できる。
- `docs/windows-app.md` の「版の上げ方」は、「タグを打って push するだけ」の 3 行に書き換える。

### 置き場所（コード）

- `src/aiseed_weather/services/usage_report.py`（Flet なし）:
  - `async def send_if_due(settings, *, client: httpx.AsyncClient | None = None, today: date | None = None) -> bool`
    - `settings.usage_report` が False なら何もしない。
    - その日にもう送っていれば何もしない。
    - それ以外は送って、成否を返す。
  - 受け口の URL は定数 `ENDPOINT = "https://analytics.aiseed.dev/v1/app"`。
  - `usage.json` の読み書きもここに置く。小さな自分のファイルなので、`user_settings.py` の流儀に揃える。
  - 例外はすべてここで受けて `logger.info`。呼ぶ側に投げない。
- 呼ぶ所: `main.py` の、画面を出した後（`_configure` か App の最初の effect）。
  - 設定を読み込めた（`status` が `ok`）ときだけ呼ぶ。
  - 設定ファイルを作った直後（`created`）の起動では、送らない。利用者がまだ案内を読んでいないため。

### 試験 `tests/test_usage_report.py`

`httpx.MockTransport` を使う。本物の受け口へは送らない。

- 送る本文が、上の 4 項目の JSON と同じ形である（ほかの鍵を含まない）。
- `usage_report = False` なら送らない。
- 同じ日の 2 回目は送らない。日付が変われば送る。
- 失敗（500、例外、時間切れ）のときは記録しない。次の呼び出しで再び送る。
- `usage.json` が壊れていても落ちない（送ってから書き直す）。
- config.toml に鍵が無ければ True、`false` なら False、`"yes"` などは設定の誤り。

### 受け口側（実装ではなく、確かめること）

kaiseki の受け口は aiai-tools の持ち物。`/v1/app` は aiai-tools が作り、deb2 には「aiaiの管理者」が置く。
`KAISEKI_APPS` の設定もそちらで行う。日ごとの数は `/v1/report` の `event=app_start` で出る予定。
**アプリから Origin ヘッダーを偽って付けることはしない。**

### 知らせ（電気通信事業法の外部送信規律）

- アプリの中:
  - 初回の案内（`ConfigStatusPanel` の created のとき）に、「起動した日に 1 回、版と OS を送ります。
    config.toml の usage_report = false で止められます」を1段落足す。
  - 下の「このアプリについて」にも書く。
- サイト `WeatherStatic/templates/kaiseki.html` に「デスクトップアプリ AIseed Weather」の節を足す。
  - 今のページと同じ並びにする: 送る情報（版、OS、配布の種類、起動した日。人を見分ける物は送らない）、
    送り先（analytics.aiseed.dev）、目的（使われている台数を知る）、止め方（config.toml の usage_report）。
  - 既存の節の書き方（送る情報・目的…）に揃える。
- README と `docs/windows-app.md` にも短く書く。

---

## G. 「このアプリについて」と、Facebook・Discord へのリンク

### リンクの持ち方

`src/aiseed_weather/models/project_links.py`（定数だけ）:

```python
SITE_URL = "https://weather.time-j.net/app/"
GITHUB_URL = "https://github.com/aiseed-dev/weather"
FACEBOOK_URL = ""   # 利用者から受け取ったら入れる
DISCORD_URL = ""    # Discord のサーバーを作ったら招待リンクを入れる
```

空の URL は、画面に出さない。利用者が URL をくれたら、この 2 行を書き換えるだけで出るようにする。
サイト側も同じ値を `WeatherStatic/generate.py` の定数 `COMMUNITY_LINKS` に持つ。
アプリとサイトはパッケージが別なので、共有はしない。両方に書く。

### アプリの「このアプリについて」

- 常に見えている場所に、小さなボタンを1つ置く（`ft.Icons.INFO_OUTLINE`、tooltip「このアプリについて」）。
  - 候補は、下の「取得状況・ターミナル・ログ」の帯の右端（開閉の矢印の左）。
  - `components/app.py` を読み、ほかに自然な場所があればそちらでよい。その場合は報告する。
- 押すと `ft.AlertDialog` を開き、次を出す。
  - アプリ名と版（`__version__`）、種別（配布版 / 手元の実行）
  - リンク（`url_launcher` か、Flet 1.0.4 で外部ブラウザーを開く方法。API は get_api で確かめる）
    - 「サイト（使い方）」
    - 「GitHub」
    - 「Facebook ページ」（URL があるときだけ）
    - 「Discord（質問・話し合い）」（URL があるときだけ）
  - 利用の記録:
    - 今の状態（「送っています」/「送っていません」）
    - 送る中身の一文
    - 「設定ファイルを開く」ボタン（`user_settings.open_in_text_editor`）
    - サイトの説明（`/kaiseki/`）へのリンク
  - 出典・ライセンスの一文（ECMWF CC-BY-4.0、Open-Meteo CC-BY-4.0、気象庁）
- 文字は日本語だけ（C の方針）。

### サイト（/app/ と /app/forecast/）

- 「最新の知らせ・質問」の小さな節を足す。Facebook と Discord のリンクを、URL があるものだけ出す。
  - /app/ は「入れ方」の後。/app/forecast/ はページの終わり近く。
  - 両方とも空なら、節ごと出さない。
- 外へのリンクの記録（`out`）は、`app-pages.js` がすべての外部リンクで送っている。足す処理は無い。
- 公開は、利用者の指示があってから dev → deb2 → Cloudflare の順に行う（Opus が行う）。
  実装は、dev でテストデータで生成して見るところまで。

---

## 文書

- `aiseed-conventions`:
  - Forbidden の「Auto-downloading at app start」の近くに、例外を1行足す:
    「利用の記録（usage_report）は例外。起動した日に1回、版と OS だけを送り、何も受け取らない」。
  - models/ が読み書きしてよい自分のファイルの一覧に、`usage.json` を足す。
- `first-run-setup`: config.toml の鍵に `usage_report` を足す。
- `docs/windows-app.md`:
  - 利用の記録の節を足す。
  - リリースの手順（タグを打って push するだけ）を足す。
- README（日本語と英語）: 利用の記録と止め方を短く書く。

---

## 確かめ方

1. `PYTHONPATH=src ./.venv/bin/python -m pytest -q tests`（`test_flet_api_names.py` を含む）。
2. 手元で起動したとき、本物の受け口へ送らないようにする。自分の config で `usage_report = false` にする。
   - 送る処理は、試験（MockTransport）で確かめる。
   - 手元で本物の受け口へ送ると、作者の記録に「source」として残る。それでも試すなら、
     利用者に一言確かめてからにする。
3. 画面（Web 版で可）:
   - 「このアプリについて」が開く。
   - リンクが出る（今は Facebook・Discord とも空なので出ない。空のときに出ないことを確かめる）。
   - 状態の表示と、「設定ファイルを開く」が動く。
4. サイト: dev で `/app/`・`/app/forecast/`・`/kaiseki/` を生成し、
   - 新しい節が出ること
   - URL が空のときは「最新の知らせ・質問」の節が出ないこと
   を確かめる。
5. コミットと push は、利用者がそのセッションで指示したときだけ。
