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

## まだのこと

- 普通に起動して、取得（数値予報・ナウキャスト）と動く地図（GPU）を使えるか
- 署名していないので、初めての起動で Windows の SmartScreen が警告を出す
- `/app/` の入れ方の「Windows: まだ確かめていません」を、Release ができたら書き換える
