# ドキュメント

## 運用（動かす人が読む）

| | 内容 |
|---|---|
| [operations.md](operations.md) | **個人開発気象統計の運用手順書。**観測の収集からサイト公開まで。cron・障害時の対処・無料枠の実測 |
| [web.md](web.md) | **サイトの構成。**ページ一覧・テンプレート・CSS・ブラウザ側で動くもの |
| [operations-forecast.md](operations-forecast.md) | 数値予報の配信、観測データの R2 配布、世界天気 |
| [r2-deployment.md](r2-deployment.md) | R2 バケットの作成と公開設定 |

## 設計（変える人が読む）

| | 内容 |
|---|---|
| [data-acquisition.md](data-acquisition.md) | 気象庁のどのデータをどう取るか。利用条件と出典表示 |
| [forecast-spec.md](forecast-spec.md) | 数値予報パックの仕様 |
| [forecast-distribution.md](forecast-distribution.md) | 数値予報の配信設計 |
| [forecast-charts.md](forecast-charts.md) | 予報図の描画 |

WeatherStatic 固有の設計は同ディレクトリ内にある。

| | 内容 |
|---|---|
| [../WeatherStatic/DESIGN.md](../WeatherStatic/DESIGN.md) | サイトの構成 |
| [../WeatherStatic/DATA_CONTRACT.md](../WeatherStatic/DATA_CONTRACT.md) | データの受け渡し規約 |
| [../WeatherStatic/DATA_SOURCES.md](../WeatherStatic/DATA_SOURCES.md) | 各データの出所と形式 |
| [../WeatherStatic/STORAGE_FORMATS.md](../WeatherStatic/STORAGE_FORMATS.md) | observations.nc と weather.sqlite の形 |

## 読み物

| | 内容 |
|---|---|
| [article-flet-as-modern-vb.md](article-flet-as-modern-vb.md) | Flet を現代の VB として使う |

## 最初に読むもの

**動かす**なら [operations.md](operations.md) の「全体の形」と「公開の手順」。
この 2 節で、何がどこで動いていて、どうすれば公開できるかが分かる。

**サイトを直す**なら [web.md](web.md)。どのページをどのテンプレートが作り、
何がサーバー側で何がブラウザ側かが分かる。
