# 運用手順書（個人開発気象統計）

観測データの収集からサイト公開までの手順。数値予報・観測データ配布・
世界天気は [operations-forecast.md](operations-forecast.md)。

設計は [WeatherStatic/DESIGN.md](../WeatherStatic/DESIGN.md)、データの出所は
[data-acquisition.md](data-acquisition.md)、保存形式は
[WeatherStatic/STORAGE_FORMATS.md](../WeatherStatic/STORAGE_FORMATS.md) を参照。

## 全体の形

三つの経路がある。**どれが止まると何が困るか**が違うので、混ぜない。

| 経路 | 取得者 | 取得元 | 頻度 | 用途 | 止まると |
|------|--------|--------|------|------|----------|
| 速報 | Cloudflare Worker → R2 | 地点別 10 分値 | 10 分ごと | 実況ページ・Flet 版 | 実況が古くなる。10 日で取り返せなくなる |
| 統計 | **deb2 が直接** | map 毎正時 ＋ 確定値 CSV | 1 日 1 回（1 時以降） | `observations.nc` | 7 日以内の再開なら自力で埋まる |
| 公開 | deb2 | 自分の生成物 | 10 分ごと・毎時 | Cloudflare Pages | サイトが更新されない |

**速報と統計を分けてある。** 10 分値は実況用の速報で、統計には使わない。
分けていないと、10 分値の収集が止まったときに統計まで欠ける。
2026-08-29 に実際に 2 日止まり、実況ページだけが古いまま公開された。

deb2 は生産者であって配信者ではない。押し出したあとは落ちていても
利用者は困らない。**deb2 を公開する必要はない**（アウトバウンドのみ）。

## 道具と置き場所

道具は動く場所で分かれている。手元専用のものは deb2 へ送らない
（向こうに置かなければ向こうで動かせない）。

| 場所 | 道具 | すること |
|------|------|----------|
| dev | `make_testdata.py` | 作り物のデータを置く。見た目の確認用 |
| dev | `sync_to_server.py` | ソースを送る。**自分と release.py は送らない** |
| dev | `deploy_worker.py` | Worker を Cloudflare へデプロイする |
| dev | `release.py` | deb2 の生成物を取り寄せて公開（臨時・確認用） |
| deb2 | `fetch_points.py` | Worker を呼んで 10 分値を集める（速報） |
| deb2 | `fetch_amedas_mirror.py` | 10 分値を複製し半月 NetCDF へ封入 |
| deb2 | `accumulate.py` | map 毎正時と確定値 CSV から `observations.nc` を作る（統計） |
| deb2 | `fetch_data.py` | 現在値・予報・現在天気 |
| deb2 | `build_site.py --publish` | 生成・点検・**定期公開**。cron はこれ |
| Worker | `workers/amedas-point/` | 渡された地点を取り、渡された名前で R2 に 1 本置く |

**定期公開は deb2 が行う。** サイトは 10 分ごとに更新されるので、生成だけ
して誰も上げない形は成り立たない。`release.py`（手元から）は臨時の経路で、
deb2 を経由せず確かめたいときや cron が止まっているときに使う。

**Worker に判断を持たせない。** いつ・何を取るか、失敗をどう呼び直すかは
すべて deb2 が決める。Worker は重い取得を肩代わりする手足に徹する。
そうしないと、地点の増減のたびに Worker の再デプロイが要る。

## 公開の手順（3 段。飛ばさない）

```bash
# 1. dev でテストデータで確認
cd ~/dev/weather/WeatherStatic
./.venv/bin/python make_testdata.py --force
./.venv/bin/python build_site.py

# 2. ソースを送って deb2 で実データで確認
./.venv/bin/python sync_to_server.py            # 下見
./.venv/bin/python sync_to_server.py --apply
ssh deb2 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py'

# 3. deb2 から Cloudflare へ
ssh deb2 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py --dry-run'
ssh deb2 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py --publish'
```

そのあとは cron が 10 分ごと・毎時に同じことを行う。手順を踏むのは、
**コードを変えたとき**だけ。データの更新は cron に任せる。

公開の前に点検を通る必要がある。`_meta.source` が `TESTDATA` なら拒む。
ページ数が目安（500）を下回るときも拒む。生成が途中で失敗したものを
上げないため。

`release.py`（手元から）は臨時の経路。deb2 の `public/` を取り寄せて上げる。
cron が止まっているときや、deb2 を経由せず確かめたいときに使う。

`sync_to_server.py --stale` で「向こうにだけあるファイル」を調べられる。
こちらで消したものが残っていると、古いコードからデプロイしてしまう。

## cron（deb2）

**cron の設定は運用者が行う。** ここに載せるのは照合用の一覧で、
道具や手順書がこれを自動で登録することはしない。二重登録すると同じ処理が
並走し、`observations.nc` の書込が後勝ちで失われる。
`crontab -l` と見比べて、足りないものだけを足すこと。

```cron
# 10分毎: 10 分値を複製し半月 NetCDF へ封入
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_amedas_mirror.py >> $HOME/dev/weather/logs/amedas_mirror.log 2>&1
# 10分毎: 現在値だけ更新（トップページが読む public/data/current.json）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_data.py --current-only >> $HOME/dev/weather/logs/current.log 2>&1
# 10分毎: 地点別 10 分値を集め、今日の最高・最低を重ね、サイトを作って公開
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && sleep 90 && { ../.venv/bin/python fetch_points.py; ../.venv/bin/python fetch_data.py --points-only && ../.venv/bin/python build_site.py --publish; } >> $HOME/dev/weather/logs/status.log 2>&1
# 毎時52分: 最新CSV・予報・現在天気 → サイト再生成 → 公開
52 * * * * cd $HOME/dev/weather/WeatherStatic && { ../.venv/bin/python fetch_data.py && ../.venv/bin/python build_site.py --publish; } >> $HOME/dev/weather/logs/publish.log 2>&1
# 日次 1:30: 統計の蓄積（気象庁の 1 時更新の後）
30 1 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python accumulate.py >> $HOME/dev/weather/logs/accumulate.log 2>&1
# 毎月2日 03:30: 前月分を etrn 確定値で置換
30 3 2 * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python backfill_etrn.py --from $(date -d "-1 month" +\%Y-\%m) --to $(date -d "-1 month" +\%Y-\%m) --force >> $HOME/dev/weather/logs/etrn_monthly.log 2>&1
# 日次: 投票集計（Workers+KV 版。要 VOTES_KV_NAMESPACE_ID）
15 1 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python aggregate_votes.py --kv
```

**入れる順番。** 前提が揃っていない行を入れると、毎回失敗するだけで何も
進まない（失敗は log に溜まるだけで気づきにくい）。

| 段 | 前提 | 入れる行 |
|---|---|---|
| 1. 収集だけ | なし | `fetch_amedas_mirror.py`、`fetch_data.py --current-only`、`accumulate.py`、月次 `backfill_etrn.py`、毎時は公開なしの `fetch_data.py` 単独 |
| 2. 公開 | `~/.config/cloudflare/pages.env`（Pages + R2） | 毎時の行を上の一覧のものにする。10 分ごとの行は `fetch_points.py;` を抜いた形（`{ fetch_data.py --points-only && build_site.py --publish; }`） |
| 3. 速報 | Worker のデプロイと `WEATHER_WORKER_*` の 2 行 | 10 分ごとの行を上の一覧のもの（先頭に `fetch_points.py;`）にする |

**コマンドは `{ …; }` でまとめてからログへ送る。** `a && b >> log` と書くと
ログに入るのは `b` の出力だけで、`a` の出力やエラーは cron のメールに流れて
見えなくなる（2026-10-05 まで一覧がこの形だった）。

10 分ごとの行の `fetch_points.py` の後ろが `;` なのは、一部のエリアが取れずに
終了コード 1 になっても、取れた分で生成と公開を続けるため。

**生成と公開は `build_site.py` 自身が `public.lock` で直列にする。** 毎時 52 分の
行と 10 分ごとの行（50 分 ＋ 90 秒待ち）はほぼ同時に走るが、片方は待たされる。
15 分待って取れなければその回は見送る。

投票集計は KV を用意してから。

**ロックはスクリプト自身が取る。** `observations.nc` は「コピー → 更新 →
rename」で置き換えるため、同時実行すると後勝ちで書込が失われる。以前は
cron 側の `flock` に頼っていたが、手動実行の手順から簡単に抜け落ちる
（実際に抜けた）。`weatherlib/storelock.py` を accumulate / backfill_daily /
backfill_etrn が自前で使う。**cron に flock を書く必要はない。**

- ロックファイルは `WeatherStatic/store.lock`（`WEATHER_STORE_LOCK` で変更可）
- accumulate は 40 分待って取れなければその回を見送る（7 日窓なので次回が拾う）
- バックフィルは既定 1 時間待つ。その間 accumulate は見送られる

## いつ何を取りに行くか

### 統計（accumulate.py）

気象庁の更新は 1 日 1 巡（[更新時刻](https://www.data.jma.go.jp/stats/data/mdrr/man/update_k.html)）。

| 時刻 | 内容 |
|------|------|
| 1 時頃 | 10 分〜日ごとの値・前日までの順位値 |
| 2 時頃 | 官署の速報値（前日まで） |
| 3 時頃 | アメダスの速報値 |
| 14 時頃 | 官署の確定値 |

**1 日 1 回、7 日窓をまるごとさらう。**

- 確定値 CSV … 前日〜7 日前 × 2 要素 ＝ 14 リクエスト
- map 毎正時 … 7 日 × 24 ＝ 168 本（取得済みでも更新回が変われば取り直す）

窓を狭めてはいけない。前々日と 7 日前だけに絞ると、8 日以上止まった日は
永久に欠ける。7 日窓を毎日さらえば、止まっても再開時に自力で埋まる。

**区切りは日付ではなく直近の 1 時。** 日付で区切ると、0 時台に走った回が
「その日ぶん」を消化してしまい、1 時の更新で入った値が翌日まで取り込まれない。
0 時台の実行は前日 1 時の区切りに属させる。

値が変わっていれば `correction_log` に残る。

### 速報（fetch_points.py）

10 分値は**約 10 日で気象庁から消える**（実測: 10 日前 200 / 11 日前 404）。
etrn から後追いできるのは日別の気温と降水だけで、湿度・気圧・視程・風・
10 分降水は二度と取れない。

- 気象庁の `latest_time.txt` で最新スロットを知る（1 リクエスト）
- エリアごとに Worker を呼ぶ。地点の多い県は 1 回 30 地点で切り、
  `{area}-1.json` / `-2.json` と**別ファイル**に置く
- Worker は R2 に置いたのと同じバイト列を応答で返す（`"echo": true`）。
  deb2 はそれを手元の `public_amedas/point/` に書く。R2 の公開設定も
  取り寄せも要らない。手元は直近 3 日分だけ残す（1 日 約 110MB）
- 失敗したエリアは呼び直す（結果がエリア単位で返る）
- 地点別に無い要素（積雪・天気）は map を 1 本取って補う

**今日の最高・最低は 10 分ごと。** 気象庁の最新値 CSV は 1 時間ごと（毎時
50 分頃に前の正時の分）だが、地点別 10 分値には「その時刻までの今日の
最高・最低」と起時（UTC）が入っている。`fetch_data.py` は CSV の表を
`data/today_rct.csv` に元として残し、地点別の値を重ねたものを `today.csv`
にする。10 分ごとの行は `--points-only` で、CSV を取らずに重ね直すだけを行う。
重ねるのは CSV と同じ観測日で、CSV より新しいスロットだけ（0 時の CSV は
前日 24 時の確定値なので、1 時台の CSV が出るまでは CSV のまま）。
10 分ごとの気温の最大では代わりにならない（最高は 10 分の間にも出る）。

`weather` は毎正時のスロットにしか入らない（実測: 正時 150 地点・非正時 0）。
要素の一覧は持たず、来たものをそのまま重ねる。**欠測のとき要素はキーごと
来ない**ので、一覧を決め打ちすると判断を誤ったときに黙って欠ける。

### 地点別と map の違い（2026-08-29 実測・A〜G の 12 地点）

| | 要素 |
|---|---|
| 地点別のみ | `gust` `gustDirection` `gustTime` `maxTemp` `maxTempTime` `minTemp` `minTempTime` |
| map のみ | `snow` `snow1h` `snow6h` `snow12h` `snow24h` `weather` |
| 共通 | 気温・湿度・気圧 2 種・降水 4 種・日照 2 種・風・風向・視程 |

包含関係ではない。片方だけでは足りないので、`weatherlib/pointstore.py` が
両者を合流させ、消費側には map と同じ形だけを見せる。

## Cloudflare の無料枠と実測

| 制限 | 上限 | 実測 |
|------|------|------|
| Worker サブリクエスト/実行 | 50 | fetch 30 ＋ put 1 ＝ 31 |
| Worker 実行/日 | 100,000 | 72 呼び出し × 144 ＝ 10,368 |
| Worker CPU | 10ms | 本文を復号しないので余裕。復号すると 407KB で parse 3.9ms |
| R2 Class A（書込）/月 | 1,000,000 | 72 × 144 × 30 ＝ 31 万 |
| R2 Class B（読出）/月 | 10,000,000 | 端末が読むぶん |
| R2 容量 | 10 GB | 生 JSON 10 日で約 350MB |
| Pages ファイル数 | 20,000 | 4,186 |
| Pages 1 ファイル | 25 MiB | 最大 0.40 MiB |

**R2 の put もサブリクエストに数えられる**（"A subrequest is any request a
Worker makes using the Fetch API or to Cloudflare services like R2, KV, or
D1."）。地点ごとに置くと fetch N ＋ put N ＝ 2N になり 25 地点で頭打ち。
エリア単位にまとめる理由はここにもある。

気象庁へのリクエスト（1 日）:

| | 回数 | 誰が |
|---|---|---|
| 地点別 10 分値 | 1,286 × 144 | Worker |
| map（速報の補完） | 144 | deb2 |
| map（統計の時別） | 168 | deb2 |
| 確定値 CSV | 14 | deb2 |

## デプロイ（cf-publish。wrangler は使わない）

| 対象 | コマンド |
|------|----------|
| サイト | `python WeatherStatic/release.py --publish`（手元から） |
| 収集 Worker | `python WeatherStatic/deploy_worker.py --apply` |
| アメダスミラー | `cf-publish r2 sync WeatherStatic/public_amedas weather-amedas` |
| 予報パック | `cf-publish r2 sync ~/wxpub/forecast weather-forecast/forecast --delete` |

`deploy_worker.py` はどのディレクトリから実行してもよい。既定は下見で、
`--apply` を付けたときだけ上げる。合言葉がまだ無ければ `--init-token` で作る
（値は表示しない）。デプロイ後に `workers.dev` の URL を拾って `pages.env` に
書き戻し、deb2 へ足す 2 行を表示する。

R2 バケット `weather-amedas` は `wrangler.toml` に書いてあるので、無ければ
cf-publish が作る（トークンに `Workers R2 Storage: Edit` が要る）。

**前提: アカウントで R2 を有効にしておく**（ダッシュボードの「R2 Object
Storage」。無料枠でも一度有効化が要る）。未有効だと API はエラー 10042 の
403 を返し、cf-publish はそれを「トークンの権限不足」と表示する。
`deploy_worker.py` は上げる前に読み取りだけの API で Workers と R2 に届くかを
点検し、Cloudflare の本当の理由を出す（2026-10-05、権限は揃っていたのに
R2 未有効で止まった）。

**cf-publish 0.3.1 に Service Binding の対応は無い**（`--service` は存在
しない）。Worker が別の Worker を呼ぶ構成はこの道具ではデプロイできない。
`wrangler.toml` から拾う既定にも `[[services]]` は含まれない。

**R2 に独自ドメインは要らない**（収集には）。Worker → R2 はバインディング、
deb2 → Worker は `*.workers.dev` で足りる。ドメインが要るのは、ブラウザや
Flet が HTTP で読みに行くときだけ。

## 資格情報の置き場所（値は書かない）

**分ける軸はサービスではなくマシン。** トークンが漏れる単位はマシンなので、
同じマシンに 3 つ置いても被害範囲は変わらない。各マシンが実際に行う操作から
必要な権限を決める。

| マシン | すること | 要る権限 |
|--------|----------|----------|
| dev | `deploy_worker.py`（Worker のデプロイ・バケット作成） | `Workers Scripts: Edit` ＋ `Workers R2 Storage: Edit` |
| dev | `release.py`（臨時の公開・確認） | `Cloudflare Pages: Edit` |
| deb2 | `build_site.py --publish`（**定期公開**） | `Cloudflare Pages: Edit` |
| deb2 | `cf-publish r2 sync`（予報パック・過去観測） | `Workers R2 Storage: Edit` |

トークンは 1 マシン 1 本。dev は上の 2 行ぶんを合わせた権限、deb2 は下の
2 行ぶんを合わせた権限を持たせる。

**deb2 にも `Pages:Edit` が要る。** サイトは 10 分ごとに更新されるので、
定期公開は deb2 が行う。手元からしか上げられない形にすると、dev の電源が
入っているときしか site が更新されない。

置き場所（`~/.config/cloudflare/`。cf-publish は `pages.env` を既定で読む）:

**1 マシン 1 ファイル。** `~/.config/cloudflare/pages.env` に集める
（cf-publish が既定で読む場所でもある）。

| 場所 | 中身 |
|------|------|
| dev | `CLOUDFLARE_API_TOKEN`（Pages ＋ Workers ＋ R2）／`CLOUDFLARE_ACCOUNT_ID`／`WEATHER_WORKER_TOKEN` |
| deb2 | `CLOUDFLARE_API_TOKEN`（Pages ＋ R2）／`CLOUDFLARE_ACCOUNT_ID`／`WEATHER_WORKER_TOKEN`／`WEATHER_WORKER_URL` |

`WEATHER_WORKER_TOKEN` は**両マシンで同じ値**（Worker 側の secret と deb2 が
送る値が一致していないと 403 になる）。`deploy_worker.py` が dev のこの値を
そのまま Worker の secret にするので、deb2 へは同じ値を書き写す。

**ファイルごと配らない。** 向こうには向こうの資格情報が入っている。足すのは
`WEATHER_WORKER_URL` と `WEATHER_WORKER_TOKEN` の 2 行だけ。

**二種類の秘密を混ぜない。**

- Cloudflare API トークン … デプロイする権限。アカウントを操作できる
- `WEATHER_WORKER_TOKEN` … deb2 だけが Worker を呼べるようにする合言葉

後者に前者を流用しない。合言葉は deb2 の設定と Worker の環境の両方に置かれ、
呼び出しのたびに HTTP ヘッダで飛ぶ。漏れたときに失うものを「その Worker を
呼べる」だけに留める。

Worker 側の secret と deb2 が送る値は同じでなければならない。手で二度打つと
食い違うので、`deploy_worker.py` が dev の `pages.env` の値をそのまま
secret にする。

## 障害時・再開

| 症状 | 見るところ | 対処 |
|------|-----------|------|
| 実況ページが古い | `logs/points.log` `logs/status.log` | 収集が止まっている。`fetch_points.py --dry-run` で設定を確認 |
| 「生成できなかった区画が N 件」 | 同上 | 10 分値が無い。復旧するまで公開しない（`build_site.py` が止まる） |
| 10 分値に穴 | `public_amedas/map/` の最新 | 10 日以内なら `fetch_amedas_mirror.py` を繰り返す（1 回 60 スロットまで） |
| 統計に穴 | `logs/accumulate.log` | 7 日以内なら次回の実行で埋まる。それ以上なら `backfill_etrn.py` |
| `うち気象庁へ退避` が恒常的に出る | `logs/amedas_mirror.log` | Worker が動いていない合図 |
| backfill_etrn が途中で止まった | — | 同じコマンド再実行（ingest_log で続きから） |
| 予報 office が 404 | `weatherlib/jma.py` | `OFFICE_REMAP` 参照（014030→014100 等の統合例外） |
| JMA から 403/429 | — | しばらく止める。恒常なら `jma.py` の `MIN_INTERVAL` を増やす |
| チャートの日本語が豆腐 | — | Noto CJK フォント（fonts-noto-cjk）を確認 |
| publisher が途中で止まった | — | 同じコマンド再実行（manifest で続きから、GRIB もキャッシュ） |
| Pages で _headers が効かない | — | cf-publish 0.1.1 以上を使う（0.1.0 は資産扱いのバグ） |

**古いものを公開しないための関門。** `generate_status.py` は 10 分値が無くて
生成を飛ばしたら終了コード 1 を返し、`build_site.py` がそこで止まる。
黙って飛ばすと前回の生成物が残ったまま公開され、古い実況が出続ける。

## 観測ストアの由来

観測ストア（`store/observations.nc` ＋ `weather.sqlite`）の正本は
**deb2（192.168.100.3）**。2026-08-26 に tgsvr で稼働を始め、
2026-10-01 に deb2 へ移した。

- 初期データ: 旧 WeatherCore の pg_dump（weather.gz）の jma_daily を
  `backfill_daily.py` で投入済み（1880-11-01〜2022-03-14、69.1M セル）
- ダンプ末尾 5 日分（2022-03-10〜14）は速報値だったため etrn から `--force`
  再取得して置換済み（値訂正 8 件・品質フラグ更新 5 件・欠測補完 10 件）
- ギャップ（2022-04〜2026-07）は `backfill_etrn.py` で取得
- 進行中の月は etrn 対象外のため、月初の穴は毎月 2 日の月次 cron が埋める

### tgsvr → deb2 移行の記録（2026-10-01）

- store / public_amedas / master / data を rsync で移した。store は
  `store.lock` を取って取得し、**md5 が両端で一致**することを確認した
- コードは git で揃えた（deb2 には git がある。tgsvr には無かった）
- 2026-10-05 まで手順書・道具・コミットメッセージでは誤って「dev2」と
  書いていた（ホスト名は deb2）。dev の `~/.ssh/config` では `deb2` が
  正式名で、`dev2` も別名として残してある
- **tgsvr の蓄積は 2026-08-30 で止まっていた。** crontab が venv 統一前の
  `./.venv/bin/python` を指したままで、毎時 exec 失敗していた
  （cron のパスは手順書の一覧と必ず照合すること）。9 月の時別値・10 分値は
  気象庁の保持期間を過ぎて回復不能。**日別の最高・最低は etrn から回復できる**:
  `backfill_etrn.py --from 2026-08 --to 2026-09 --force`

### 欠測センチネル修復の記録（2026-10-02）

`repair_sentinels.py` で観測ストアを修復した（2026-10-03 に再検証して
やり直した版が正）。修復後の極値は tmax 41.8℃ / tmin -41.0℃（＝日本記録）。
修復前後を全変数・全セルで突き合わせ、値の変更は欠測化だけであること、
時別値・次元・地点表が不変であることを確認済み。

- **値 184,304 セルを欠測に**: ダンプ取込の甘いガードで入った欠測
  センチネル 181,745（tavg 37,375 / tmax 37,185 / tmin 37,201 /
  precip 69,984）＋ 蓄積が書いたゴミ列 2,559（tmax/tmin の
  2026-08-27〜29、precip の 08-19。原因は下記バグ）
- **品質・起時の孤児 9,045 万セルを欠測に**: 値が欠測なのに品質・起時だけ
  乱数が入っていたセル。大半は初回ダンプ取込が tavg_q / precip_q に
  書き戻した乱数（同じバグ）。2026-08-26 のゴミ列もこの形
- **precip_none を作り直し**: 乱数 4,745 万セルを欠測に戻し、乱数に
  阻まれて入っていなかった本物の 0/1 を 767 万セル、ダンプから復元
- 値のあるセルの品質は元から正しい（ダンプと違う 37 セルは etrn が
  速報の品質を確定に更新したもので、そのまま残す）
- 修復前の store は `~/backup/weather/20261002/`（dev）に残してある

2026-10-04 の独立検算（修復済みファイルに対して）:

- ダンプの有効値 約 6,900 万セルのうち、修復で消えたものは 0。ダンプと
  違う値は 2022-03 の中だけ（etrn は月単位で取り直すため、末尾 5 日でなく
  3 月全体が確定値で上書きされている。修復前から存在）
- 2026-08-20〜29 の日別 tavg・降水量（品質なし）・日照は、時別値から
  行読みで計算し直した値と全件一致
- 2026-08-19 の降水 36 セルは「−32768 ＋ 時別合計」で、欠測を値として
  足し込んだ合計の指紋だった（現行コードでは作れない形。稼働初期の
  一度きりの処理とみられ、原因は特定できていない）。この日は 20 時以降
  しか時別値の無い部分日なので、日別値は全行欠測が正しい

2026-10-05、9 月回復（`backfill_etrn.py --from 2026-08 --to 2026-09 --force`）の後:

- 8〜9 月の気温は 914〜916 地点・降水は 913〜915 地点で埋まった
  （08-20〜29 の降水が 1,285 地点あるのは、時別値のある日だけ雨量計の
  日降水量が集計されるため）
- ゴミ列の破片 51 セルが、気温を観測していない雨量計の行に残っていた
  （08-28 tmin 34・08-29 tmax 15 / tmin 2。品質 1〜5 と妥当な起時を持ち、
  修復の選別を通っていた。etrn は気温のページが無い地点を上書きしない）。
  前後 45 日に同じ要素の値が無い「孤立値」として `clean_isolated.py` で
  欠測に戻す

### libnetcdf の読み出しバグと守るべきこと（2026-10-02 発見、10-03 訂正）

**変数自身の書込み済み実寸より unlimited 次元が大きいとき、実寸を越える
読みは、ずれた配列と手つかずのバッファ（未初期化メモリ）を、エラーなしで
返す。** 原因は libnetcdf の `NC4_get_vars`（`libhdf5/hdf5var.c`）の
fill 処理で、実データを切り詰めた形のまま先頭に詰め、その後ろに fill を
「各次元の不足数（0 なら 1）の積」個だけ置く。unlimited 次元が先頭の 1 つ
だけなら正しいが、この store のように 2 つあると壊れる。ソースから導いた
予測は、目印入りバッファで C を直接呼んだ 9,028 通りの読みと全件一致した
（2026-10-04）。netCDF4-python のせいではなく、main でも未修正。
libnetcdf 4.9.2〜4.10.1 で確認しており、ピン留めでは直せない。
ncdump は行単位で読むので正しく見える。netCDF4-python 経由で総当たりすると、
書かれなかった領域にたまたま正しい値が残って壊れた読みを見逃すことがある。
回帰テストは `tests/test_nc_read_bug.py`。

| 読み方 | |
|---|---|
| 1 セル、1 行（1×n） | 常に正しい |
| 列（n×1） | **列が日付方向の実寸より外だと壊れる** |
| 2 次元 | 実寸を越えると壊れる |
| 実寸＝次元のとき | 全て正しい |

次元は「どれか 1 つの変数が書いた」だけで伸びる。だから同じ実行の中で、
ある変数が新しい日を書いた直後に別の変数のその日を列で読むと実寸外になる。
`update_daily_extreme` はその列を読んで書き戻すので、**CSV に載っていない
地点の行をゴミで埋めていた**（稼働開始の 2026-08-26 から毎日）。

守ること:

- ストアを読む直前に `ncstore.normalize_extents` で全変数の実寸を次元に
  揃える。`NcStore` の read-modify-write（`update_daily_extreme` /
  `aggregate_day`）と `close()`、`backfill_daily.py` に入れてある
- ストアを直接触る道具を書くときは、1 行読みか 1 セル読みにする。
  `var[:]` や列読みで全体を舐めない（解析結果そのものが狂う）
- 2026-10-02 に入れた「close() のときだけ揃える」は不十分だった
  （閉じた後の読み手は守れるが、同じ実行の中の読み書きは守れない）

## バックアップ（deb2 → dev）

tgsvr は main PC に転用する予定なので、転用後は観測データの実コピーが
**deb2 の 1 か所だけ**になる。日別値は weather.gz ＋ etrn でほぼ再建できるが、
**時別値・10 分値の蓄積（store と data）は消えたら戻らない**。
量は小さい（store 83MB ＋ data ＋ master ＋ public_amedas ≒ 170MB）ので、
日付つきの世代で丸ごと取る。

dev で実行:

```bash
d=~/backup/weather/$(date +%Y%m%d)
mkdir -p "$d"
ssh deb2 'flock ~/dev/weather/WeatherStatic/store.lock \
    tar -C ~/dev/weather/WeatherStatic -cf - store data master public_amedas' \
    | tar -xf - -C "$d"
```

- ロックを取るのは、書込ジョブ（copy→rename）の最中に写すと半端なファイルを
  拾うため。ロックの実体は `WeatherStatic/store.lock`
  （`weatherlib/storelock.py` の既定。リポジトリ直下ではない）
- 頻度は週 1 回を目安に手で。自動化するなら **dev 側の** cron に入れる
  （deb2 の crontab は収集系の台帳なので混ぜない）
- 古い世代の削除は人が決める。道具からは消さない

復元（deb2 の cron を止めてから。既存を上書きする）:

```bash
tar -C "$d" -cf - store data master public_amedas \
    | ssh deb2 'tar -xf - -C ~/dev/weather/WeatherStatic'
```

## まだ手つかず

- **Worker が未デプロイ**。デプロイするまで地点別データは集まらない。
  その間は `pointstore` が map ミラーへ退避する（移行のための仮の道）
- `fetch_amedas_mirror.py` が map のまま。地点別へ移すのは別の塊
- 実況ページ 1,293 枚を 10 分ごとに作り直している（43MB）。R2 直読みにすれば
  この山は消えるが、検索の入口も消える。分け方の検討が要る

数値予報・観測データ配布・世界天気は
[operations-forecast.md](operations-forecast.md) を参照。
