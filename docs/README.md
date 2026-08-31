# ドキュメント

## 二つの製品

| 製品 | 誰のため | 中身 |
|------|----------|------|
| **AIseed Weather**（デスクトップ） | **普通の人はこちら。** | Flet 製。ECMWF・ERA5・JMA のデータを利用者が手元で取得して描く。気象業務法の制約を受けない（予報も扱える） |
| **個人開発気象統計**（Web） | 恐らく自分専用 | 気象庁の観測データだけの静的サイト。3,132 ページ |

Web は「気象庁の観測をどこまで個人が扱えるか」の実証でもあり、その裏で動く
収集の仕組み（tgsvr ＋ Cloudflare）が、**アプリが読む R2 のデータ**
（アメダスミラー・予報パック・過去観測 NetCDF）も作る。サイト自体の利用者が
自分だけでも、データ基盤としては両方を支えている。

## データ基盤（tgsvr ＋ Cloudflare。両製品を支える）

| | 内容 |
|---|---|
| [operations.md](operations.md) | **運用手順書。**観測の収集からサイト公開まで。cron・障害時の対処・無料枠の実測 |
| [operations-forecast.md](operations-forecast.md) | 数値予報の配信、観測データの R2 配布、世界天気 |
| [data-acquisition.md](data-acquisition.md) | 気象庁のどのデータをどう取るか。利用条件と出典表示 |
| [r2-deployment.md](r2-deployment.md) | R2 バケットの作成と公開設定 |

## Web（個人開発気象統計）

| | 内容 |
|---|---|
| [web.md](web.md) | **サイトの構成。**ページ一覧・テンプレート・CSS・ブラウザ側で動くもの |
| [../WeatherStatic/DESIGN.md](../WeatherStatic/DESIGN.md) | 静的ジェネレータの設計 |
| [../WeatherStatic/DATA_CONTRACT.md](../WeatherStatic/DATA_CONTRACT.md) | データの受け渡し規約 |
| [../WeatherStatic/DATA_SOURCES.md](../WeatherStatic/DATA_SOURCES.md) | 各データの出所と形式 |
| [../WeatherStatic/STORAGE_FORMATS.md](../WeatherStatic/STORAGE_FORMATS.md) | observations.nc と weather.sqlite の形 |

## デスクトップアプリ（AIseed Weather）

| | 内容 |
|---|---|
| [../CLAUDE.md](../CLAUDE.md) | 製品の狙いとデータ層の役割分担 |
| [forecast-spec.md](forecast-spec.md) | アプリが読む数値予報パックの仕様 |
| [forecast-distribution.md](forecast-distribution.md) | 予報パックの配信設計 |
| [forecast-charts.md](forecast-charts.md) | 予報図の描画 |

利用者向けの文書は **Web が載せる**: `/App/`（紹介）と `/App/Develop/`
（開発マニュアル）。テンプレートは
[../WeatherStatic/templates/app/](../WeatherStatic/templates/app/) にあり、
内容の正本はリポジトリの README / AGENTS / CLAUDE。向こうを変えたら
ページも直す。

## 読み物

| | 内容 |
|---|---|
| [article-flet-as-modern-vb.md](article-flet-as-modern-vb.md) | Flet を現代の VB として使う |

## 最初に読むもの

**動かす**なら [operations.md](operations.md) の「全体の形」と「公開の手順」。
この 2 節で、何がどこで動いていて、どうすれば公開できるかが分かる。

**サイトを直す**なら [web.md](web.md)。どのページをどのテンプレートが作り、
何がサーバー側で何がブラウザ側かが分かる。

**アプリを作る**なら [../CLAUDE.md](../CLAUDE.md) と
[forecast-spec.md](forecast-spec.md)。
