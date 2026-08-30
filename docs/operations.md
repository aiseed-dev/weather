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
| 統計 | **tgsvr が直接** | map 毎正時 ＋ 確定値 CSV | 1 日 1 回（1 時以降） | `observations.nc` | 7 日以内の再開なら自力で埋まる |
| 公開 | tgsvr | 自分の生成物 | 10 分ごと・毎時 | Cloudflare Pages | サイトが更新されない |

**速報と統計を分けてある。** 10 分値は実況用の速報で、統計には使わない。
分けていないと、10 分値の収集が止まったときに統計まで欠ける。
2026-08-29 に実際に 2 日止まり、実況ページだけが古いまま公開された。

tgsvr は生産者であって配信者ではない。押し出したあとは落ちていても
利用者は困らない。**tgsvr を公開する必要はない**（アウトバウンドのみ）。

## 道具と置き場所

道具は動く場所で分かれている。手元専用のものは tgsvr へ送らない
（向こうに置かなければ向こうで動かせない）。

| 場所 | 道具 | すること |
|------|------|----------|
| dev | `make_testdata.py` | 作り物のデータを置く。見た目の確認用 |
| dev | `sync_to_tgsvr.py` | ソースを送る。**自分と release.py は送らない** |
| dev | `deploy_worker.py` | Worker を Cloudflare へデプロイする |
| dev | `release.py` | tgsvr の生成物を取り寄せて公開（臨時・確認用） |
| tgsvr | `fetch_points.py` | Worker を呼んで 10 分値を集める（速報） |
| tgsvr | `fetch_amedas_mirror.py` | 10 分値を複製し半月 NetCDF へ封入 |
| tgsvr | `accumulate.py` | map 毎正時と確定値 CSV から `observations.nc` を作る（統計） |
| tgsvr | `fetch_data.py` | 現在値・予報・現在天気 |
| tgsvr | `build_site.py --publish` | 生成・点検・**定期公開**。cron はこれ |
| Worker | `workers/amedas-point/` | 渡された地点を取り、渡された名前で R2 に 1 本置く |

**定期公開は tgsvr が行う。** サイトは 10 分ごとに更新されるので、生成だけ
して誰も上げない形は成り立たない。`release.py`（手元から）は臨時の経路で、
tgsvr を経由せず確かめたいときや cron が止まっているときに使う。

**Worker に判断を持たせない。** いつ・何を取るか、失敗をどう呼び直すかは
すべて tgsvr が決める。Worker は重い取得を肩代わりする手足に徹する。
そうしないと、地点の増減のたびに Worker の再デプロイが要る。

## 公開の手順（3 段。飛ばさない）

```bash
# 1. dev でテストデータで確認
cd ~/dev/weather/WeatherStatic
./.venv/bin/python make_testdata.py --force
./.venv/bin/python build_site.py

# 2. ソースを送って tgsvr で実データで確認
./.venv/bin/python sync_to_tgsvr.py            # 下見
./.venv/bin/python sync_to_tgsvr.py --apply
ssh tgsvr 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py'

# 3. tgsvr から Cloudflare へ
ssh tgsvr 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py --dry-run'
ssh tgsvr 'cd dev/weather/WeatherStatic && ../.venv/bin/python build_site.py --publish'
```

そのあとは cron が 10 分ごと・毎時に同じことを行う。手順を踏むのは、
**コードを変えたとき**だけ。データの更新は cron に任せる。

公開の前に点検を通る必要がある。`_meta.source` が `TESTDATA` なら拒む。
ページ数が目安（500）を下回るときも拒む。生成が途中で失敗したものを
上げないため。

`release.py`（手元から）は臨時の経路。tgsvr の `public/` を取り寄せて上げる。
cron が止まっているときや、tgsvr を経由せず確かめたいときに使う。

`sync_to_tgsvr.py --stale` で「向こうにだけあるファイル」を調べられる。
こちらで消したものが残っていると、古いコードからデプロイしてしまう。

## cron（tgsvr）

**cron の設定は運用者が行う。** ここに載せるのは照合用の一覧で、
道具や手順書がこれを自動で登録することはしない。二重登録すると同じ処理が
並走し、`observations.nc` の書込が後勝ちで失われる。
`crontab -l` と見比べて、足りないものだけを足すこと。

```cron
# 10分毎: 地点別 10 分値を集める（速報）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_points.py >> $HOME/dev/weather/logs/points.log 2>&1
# 10分毎: 10 分値を複製し半月 NetCDF へ封入
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_amedas_mirror.py >> $HOME/dev/weather/logs/amedas_mirror.log 2>&1
# 10分毎: 現在値だけ更新（トップページが読む public/data/current.json）
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_data.py --current-only >> $HOME/dev/weather/logs/current.log 2>&1
# 10分毎: 実況ページを作って公開。収集の後に回す
*/10 * * * * cd $HOME/dev/weather/WeatherStatic && sleep 90 && ../.venv/bin/python generate_status.py && ../.venv/bin/python build_site.py --skip-build --publish >> $HOME/dev/weather/logs/status.log 2>&1
# 毎時50分: 最新CSV・予報・現在天気 → サイト再生成 → 公開
52 * * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python fetch_data.py && ../.venv/bin/python build_site.py --publish >> $HOME/dev/weather/logs/publish.log 2>&1
# 日次 1:30: 統計の蓄積（気象庁の 1 時更新の後）
30 1 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python accumulate.py >> $HOME/dev/weather/logs/accumulate.log 2>&1
# 毎月2日 03:30: 前月分を etrn 確定値で置換
30 3 2 * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python backfill_etrn.py --from $(date -d "-1 month" +\%Y-\%m) --to $(date -d "-1 month" +\%Y-\%m) --force >> $HOME/dev/weather/logs/etrn_monthly.log 2>&1
# 日次: 投票集計（Workers+KV 版。要 VOTES_KV_NAMESPACE_ID）
15 1 * * * cd $HOME/dev/weather/WeatherStatic && ../.venv/bin/python aggregate_votes.py --kv
```

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
- 失敗したエリアは呼び直す（結果がエリア単位で返る）
- 地点別に無い要素（積雪・天気）は map を 1 本取って補う

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
| map（速報の補完） | 144 | tgsvr |
| map（統計の時別） | 168 | tgsvr |
| 確定値 CSV | 14 | tgsvr |

## デプロイ（cf-publish。wrangler は使わない）

| 対象 | コマンド |
|------|----------|
| サイト | `python WeatherStatic/release.py --publish`（手元から） |
| 収集 Worker | `python WeatherStatic/deploy_worker.py --apply` |
| アメダスミラー | `cf-publish r2 sync WeatherStatic/public_amedas weather-amedas` |
| 予報パック | `cf-publish r2 sync ~/wxpub/forecast weather-forecast/forecast --delete` |

`deploy_worker.py` はどのディレクトリから実行してもよい。既定は下見で、
`--apply` を付けたときだけ上げる。合言葉がまだ無ければ `--init-token` で作る
（値は表示しない）。デプロイ後に `workers.dev` の URL を拾って `worker.env` に
書き戻し、tgsvr へ足す 2 行を表示する。

R2 バケット `weather-amedas` は `wrangler.toml` に書いてあるので、無ければ
cf-publish が作る（トークンに `Workers R2 Storage: Edit` が要る）。

**cf-publish 0.3.1 に Service Binding の対応は無い**（`--service` は存在
しない）。Worker が別の Worker を呼ぶ構成はこの道具ではデプロイできない。
`wrangler.toml` から拾う既定にも `[[services]]` は含まれない。

**R2 に独自ドメインは要らない**（収集には）。Worker → R2 はバインディング、
tgsvr → Worker は `*.workers.dev` で足りる。ドメインが要るのは、ブラウザや
Flet が HTTP で読みに行くときだけ。

## 資格情報の置き場所（値は書かない）

**分ける軸はサービスではなくマシン。** トークンが漏れる単位はマシンなので、
同じマシンに 3 つ置いても被害範囲は変わらない。各マシンが実際に行う操作から
必要な権限を決める。

| マシン | すること | 要る権限 |
|--------|----------|----------|
| dev | `deploy_worker.py`（Worker のデプロイ・バケット作成） | `Workers Scripts: Edit` ＋ `Workers R2 Storage: Edit` |
| dev | `release.py`（臨時の公開・確認） | `Cloudflare Pages: Edit` |
| tgsvr | `build_site.py --publish`（**定期公開**） | `Cloudflare Pages: Edit` |
| tgsvr | `cf-publish r2 sync`（予報パック・過去観測） | `Workers R2 Storage: Edit` |

**tgsvr にも `Pages:Edit` が要る。** サイトは 10 分ごとに更新されるので、
定期公開は tgsvr が行う。手元からしか上げられない形にすると、dev の電源が
入っているときしか site が更新されない。

置き場所（`~/.config/cloudflare/`。cf-publish は `pages.env` を既定で読む）:

| 場所 | ファイル | 中身 |
|------|----------|------|
| dev | `pages.env` | Pages（既存のまま） |
| dev | `worker.env` | Workers ＋ R2 ＋ `WEATHER_WORKER_TOKEN`。`deploy_worker.py` がこちらを先に見る |
| tgsvr | `pages.env` | Pages ＋ R2 ＋ `WEATHER_WORKER_URL` ＋ `WEATHER_WORKER_TOKEN` |

`worker.env` を分けるのは、既存の Pages トークンに Workers の権限を足したく
ない場合の措置。1 つにまとめてもよい（その場合 `pages.env` に全権限）。

**Cloudflare まわりの設定は `~/.config/cloudflare/` に集める。** tgsvr では
`pages.env` に資格情報と合わせて Worker の URL と合言葉を置く
（`fetch_points.py` がここを読む）。

**二種類の秘密を混ぜない。**

- Cloudflare API トークン … デプロイする権限。アカウントを操作できる
- `WEATHER_WORKER_TOKEN` … tgsvr だけが Worker を呼べるようにする合言葉

後者に前者を流用しない。合言葉は tgsvr の設定と Worker の環境の両方に置かれ、
呼び出しのたびに HTTP ヘッダで飛ぶ。漏れたときに失うものを「その Worker を
呼べる」だけに留める。

**`worker.env` を tgsvr へ丸ごと配らない。** 向こうの `pages.env` には
向こうの資格情報が入っている。足すのは `WEATHER_WORKER_URL` と
`WEATHER_WORKER_TOKEN` の 2 行だけ。

Worker 側の secret と tgsvr が送る値は同じでなければならない。手で二度打つと
食い違うので、`deploy_worker.py` が `worker.env` の値をそのまま secret にする。

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

## 観測ストアの由来（tgsvr、2026-08-26 稼働開始）

観測ストア（`store/observations.nc` ＋ `weather.sqlite`）の正本は tgsvr。

- 初期データ: 旧 WeatherCore の pg_dump（weather.gz）の jma_daily を
  `backfill_daily.py` で投入済み（1880-11-01〜2022-03-14、69.1M セル）
- ダンプ末尾 5 日分（2022-03-10〜14）は速報値だったため etrn から `--force`
  再取得して置換済み（値訂正 8 件・品質フラグ更新 5 件・欠測補完 10 件）
- ギャップ（2022-04〜2026-07）は `backfill_etrn.py` で取得
- 進行中の月は etrn 対象外のため、月初の穴は毎月 2 日の月次 cron が埋める

## まだ手つかず

- **Worker が未デプロイ**。デプロイするまで地点別データは集まらない。
  その間は `pointstore` が map ミラーへ退避する（移行のための仮の道）
- `fetch_amedas_mirror.py` が map のまま。地点別へ移すのは別の塊
- 実況ページ 1,293 枚を 10 分ごとに作り直している（43MB）。R2 直読みにすれば
  この山は消えるが、検索の入口も消える。分け方の検討が要る
- **Worker が未デプロイ**。デプロイするまで地点別データは集まらない。
  その間は `pointstore` が map ミラーへ退避する（移行のための仮の道）
- `fetch_amedas_mirror.py` が map のまま。地点別へ移すのは別の塊
- 実況ページ 1,293 枚を 10 分ごとに作り直している（43MB）。R2 直読みにすれば
  この山は消えるが、検索の入口も消える。分け方の検討が要る

数値予報・観測データ配布・世界天気は
[operations-forecast.md](operations-forecast.md) を参照。
