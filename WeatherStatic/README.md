# WeatherStatic — 個人開発気象統計

気象庁の観測データだけを扱う静的サイトと、その裏で動く収集・蓄積・配布の仕組み。
旧 WeatherCore（ASP.NET Core）を、Python の定期生成と Cloudflare Pages に移したもの。

- サイト: https://weather.time-j.net/（約 3,100 ページ。実況と今日の最高・最低は 10 分ごと。Pages の https://weather-dj7.pages.dev/ でも同じ物が見える）
- 観測データの配布: `/data/daily/`（日別、1880 年〜）・`/data/amedas/`（10 分値・1 時間値）
- 予報は扱わない（予報業務には気象業務法の許可が要る）。防災情報も扱わない

詳しい文書は [../docs/README.md](../docs/README.md) から辿れる。**動かすなら
[../docs/operations.md](../docs/operations.md)**、**サイトを直すなら
[../docs/web.md](../docs/web.md)**。

## 全体の形

```
気象庁 ──┬─ 地点別 10 分値 ── Worker（Cloudflare）── R2 ＋ deb2 の手元
         ├─ 10 分値 map JSON ─────────────────────── deb2（半月ごとの NetCDF）
         ├─ 最新の気象データ CSV（毎時）・確定値 CSV ─ deb2
         └─ 過去の気象データ（etrn、月次）──────────── deb2
                                                       │
                     観測ストア store/observations.nc ＋ weather.sqlite
                                                       │
                     generate.py / generate_status.py → public/ → Cloudflare Pages
```

- **いつ何を取るかは deb2 が決める。** Cloudflare に cron は置かない。Worker は
  頼まれた地点を取って R2 に置き、同じものを deb2 に返すだけ
- **公開は 3 段。** dev（テストデータ）→ deb2（実データ）→ Cloudflare。段を飛ばさない

## 道具

| 道具 | どこで | 何を |
|------|--------|------|
| `fetch_points.py` | deb2・10 分ごと | 地点別 10 分値を Worker 経由で集める。最大瞬間風速などの日別の記録も作る |
| `fetch_amedas_mirror.py` | deb2・10 分ごと | 10 分値 map JSON を複製し、半月ごとに NetCDF（10 分値・1 時間値）へ封入 |
| `fetch_data.py` | deb2・毎時／10 分ごと | 最新の気象データ CSV・予報・現在値。`--points-only` で今日の最高・最低を 10 分値で重ね直す |
| `accumulate.py` | deb2・毎日 1:30 | 観測ストアへの蓄積（時別値・確定値 CSV・日集計。7 日分を毎日取り直す） |
| `backfill_etrn.py` | deb2・毎月 2 日 | 前月分を etrn の確定値で置き換える |
| `build_site.py` | deb2・10 分ごと／毎時 | 生成 → 点検 → 公開（`--publish` のときだけ） |
| `export_dist.py` | deb2（生成から呼ばれる） | 観測ストアの日別値を年ごと・地点ごとの NetCDF に（`/data/daily/`） |
| `check_legacy_urls.py` | dev／deb2 | 旧サイトの URL（4,154 件）が生成したサイトでどこに着くかを点検する（公開前） |
| `make_testdata.py` | dev | 作り物のデータで手元を組み立てる（公開は拒まれる） |
| `sync_to_server.py` | dev | 作業ツリーのソースを deb2 へ送る（コミット済みなら git push → pull） |
| `deploy_worker.py` | dev | 収集 Worker のデプロイ |
| `repair_sentinels.py`・`clean_isolated.py` | dev／deb2 | 観測ストアの修復（2026-10 に使用。記録は運用手順書） |

cron の一覧と入れる順番、資格情報の置き場所、障害時の対処は運用手順書にある。

## 手元で動かす（テストデータ）

venv はリポジトリ直下に 1 つ（`../.venv`）。サーバー（deb2）は
`python3 -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt cf-publish`
で作った。開発機はデスクトップアプリと共用の環境（`environment.yml`）に、
`requirements.txt` の分を足して使っている。

```bash
../.venv/bin/python make_testdata.py
../.venv/bin/python build_site.py                          # 生成して点検（公開はしない）
./serve_preview.sh                                         # public/ をポート 8765 で配信
```

## テスト

`tests/` の各ファイルを直接実行する（例: `../.venv/bin/python tests/test_points_overlay.py`）。
NetCDF の読み出しの不具合（`tests/test_nc_read_bug.py`）、今日の最高・最低の重ね、
日別の記録、ミラーの 1 時間値と 404 の記録などを確かめる。

## データの形

| | |
|---|---|
| [DATA_CONTRACT.md](DATA_CONTRACT.md) | 取得層と生成層の受け渡し |
| [DATA_SOURCES.md](DATA_SOURCES.md) | 各データの出所と形式 |
| [STORAGE_FORMATS.md](STORAGE_FORMATS.md) | observations.nc と weather.sqlite の形 |
| [DESIGN.md](DESIGN.md) | 静的ジェネレータの設計 |

## ライセンス

コードは AGPL-3.0-or-later。データは気象庁ホームページのコンテンツで、
公共データ利用規約（第1.0版）に準拠して利用する（出典: 気象庁ホームページ。
編集・加工したものはその旨を併記）。
