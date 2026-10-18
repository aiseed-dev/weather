# Windows 版（Python ごとまとめたアプリ）

AIseed Weather を conda を使わずに動かすための配布の形。中身は `desktop/`（`flet build windows`）。
開発は今までどおり conda の環境（`environment.yml`）と `flet run`。

## なぜ作るか

`/app/`（アプリの案内のページ）を開く人は、人のアクセスの 42% が Windows で、Mac と Linux は
合わせて 3%（2026-10-15）。conda・git・端末の操作を前提にした入れ方では、ほとんどの人が試せない。

## 組み立て

| 場所 | どうやって | 何に使う |
|---|---|---|
| GitHub Actions（`.github/workflows/windows-app.yml`） | main でアプリ・試験・`desktop/` が変わったとき、と手動 | 試験（Windows で `pytest tests`）と組み立て。成果物を 14 日残す |
| 同じ（`app-v*` のタグ） | 運用者がタグを push する | 組み立てた zip（`AIseed Weather` のフォルダー）を Release（試験版）に載せる |
| 手元の仮想環境 win11 | libvirt（`qemu:///session`）のゲストエージェントで命令を送る | 組み立てて画面に出して確かめる |

`desktop/pyproject.toml` の要点:

- アプリ本体と動く地図の部品はリポジトリの物（`dev_packages`）。ほかは PyPI の出来合い。どれも
  Windows・Python 3.13 用の wheel がある
- 実行ファイルは `AIseed Weather.exe`（`[tool.flet.windows] artifact`）
- 部品の試験一式と `.pyi` を除く（`[tool.flet.cleanup]`。約 65MB 減って約 600MB）

入口（`desktop/src/main.py`）は、先に「起動しています」を出してから本体を別のスレッドで読み込む。
本体の読み込みは Windows の初回で約 40 秒かかり、その間に Flet の画面の側が待ちきれず白いままに
なったため。最初の起動はウィンドウを最大化する（保存した大きさが無いとき）。

## win11 で確かめる

- win11 は officework と共用。`C:\owcmp`、タスク `officework-ms-pdf`・`officework-ms-pdf-close`、
  Word・Excel・Office の設定、libvirt のゲストエージェントのチャンネルとドメイン名には触らない。
  使う前後に「officework管理者」のセッションに知らせる
- weather の物は `C:\aiseed-weather` の下（Python 3.13・Git・Visual Studio Build Tools 2022 は全体に導入済み）。
  組み立ては `C:\aiseed-weather\build.ps1`（Flutter などの置き場所は `C:\aiseed-weather\home`）
- 画面に出すのは予定タスク `aiseed-weather-test`（niji として対話で起動）。画面は
  `virsh -c qemu:///session screenshot win11`、クリックは `qemu-monitor-command` の `input-send-event`
- 予定タスクから起動したアプリが開いたメモ帳は、ウィンドウが出ない（予定タスクから直接起動しても同じ）。
  「設定ファイルを開く」は、普通に起動したときに確かめる
- Python の出力は `C:\Users\<名前>\AppData\Local\AIseed\AIseed Weather\console.log`

## 利用の記録（使われている台数）

起動した日に 1 回だけ、版・OS・種別を `https://analytics.aiseed.dev/v1/app` へ送る
（`src/aiseed_weather/services/usage_report.py`）。人や PC を見分ける ID（sid・vid）は付けず、
Origin ヘッダーも偽らない。日ごとの件数が、その日に使われた台数になる（受け口 `/v1/app` は
aiai-tools が作る。できるまでは断られ、アプリは黙って次の起動に回す）。

- 送る本文は `{"site": "weather.time-j.net", "version": "0.1.1", "os": "windows", "channel": "release"}`
  の 4 項目だけ。os は `windows` / `macos` / `linux`、channel は `release`（配布版）/ `source`（手元の実行）。
  HTTP の User-Agent ヘッダーだけ、`AIseed Weather/版 (OS; channel)` と正直に名乗る
- 種別は環境変数 `AISEED_WEATHER_BUILD` で決める。配布版の入口（`desktop/src/main.py`）が
  `release` を入れる。無ければ `source`
- 送った日は `~/.config/aiseed-weather/usage.json`（`last_sent`。知らせを出した日は `notice_shown`）に残す。失敗した日は残さず、
  次の起動で再び試す。失敗しても利用者には何も出さない
- 止め方は `config.toml` の `usage_report = false`。設定ファイルを作った最初の起動では送らない
- すでに `config.toml` を持ち、`usage_report` の鍵が無い人には、最初の起動で知らせの帯（設定ファイルを開くボタン付き）を
  出し、`usage.json` に `notice_shown` を記録して、その起動では送らない。次の起動から送る。鍵を書いてある人
  （`true` でも `false` でも）と、新しく設定ファイルを作った人（テンプレートに鍵がある）には出さない
- 試験（`tests/test_usage_report.py`）は `httpx.MockTransport` で、本物の受け口へは送らない。
  手元で起動して試すときも、`usage_report = false` にするか、利用者に確かめてから送る
  （送ると作者の記録に `source` として残る）
- 知らせは、アプリの初回の案内と「このアプリについて」、サイトの `/kaiseki/`

## リリース（タグを打って push するだけ）

```bash
git tag app-v0.1.1 && git push origin app-v0.1.1
```

配布版の版は **タグだけ**で決まる。リポジトリのどのファイルにも、配布版の版は書かない
（根の `pyproject.toml` の `0.0.0` と `desktop/pyproject.toml` の `version` は上げない）。

組み立て（`.github/workflows/windows-app.yml`）の最初の段 `tools/write_build_version.py` が、
タグ `app-vX.Y.Z` から `X.Y.Z` を取り出して、git の対象外の
`src/aiseed_weather/_build_version.py`（`__version__ = "X.Y.Z"`）に書く。同じ版を
`flet build windows --build-version X.Y.Z` に渡す。`X.Y.Z` が数字 3 つの形でないタグは、ここで失敗する。
アプリは `aiseed_weather.__version__`（`_build_version.py` があればその版）を読み、利用の記録にも載せる。

- タグでない組み立て（main への push・手動）の版は `0.0.0.dev0+<コミットの短い sha>`
- 手元の実行（`flet run`・`PYTHONPATH=src`）は `_build_version.py` が無く、版は `0.0.0.dev0`。
  利用の記録には `0.0.0.dev0 … source` で入るので、配布版と見分けられる
- `_build_version.py` は `.gitignore` に入っているので、`pyproject.toml` の
  `[tool.hatch.build.targets.wheel] artifacts` で wheel に入れている（`flet build` は wheel を作って入れる）

## まだのこと

- 普通に起動して、取得（数値予報・ナウキャスト）と動く地図（GPU）を使えるか
- 署名していないので、初めての起動で Windows の SmartScreen が警告を出す
- `/app/` の入れ方の「Windows: まだ確かめていません」を、Release ができたら書き換える
