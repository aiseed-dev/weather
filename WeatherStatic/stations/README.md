# 観測所情報（git で履歴を持つ）

## なぜ git か

観測所は増減し、番号も要素も変わる。その履歴こそが価値なので、
**変更履歴を持つ仕組みに置く**のが素直だった。SQLite に
`station_change` のような表を作って差分を自前で記録していたが、
git が最初からやっていることの作り直しでしかない。

    git log -p stations/amedastable.json     # いつ何が変わったか
    git log --oneline stations/              # 変更のあった日
    git diff HEAD~1 -- stations/             # 直近の変更

`master/` は再生成できる派生物の置き場（gitignore 済み）。
こちらは**原本**で、履歴を残す。

## 中身

| ファイル | 出どころ | 更新 |
|---|---|---|
| `amedastable.json` | 気象庁 `bosai/amedas/const/amedastable.json` | 現況配信の地点表。**これが正** |
| `ame_master.csv` | 気象庁 `ame_master.zip`（CP932 → UTF-8） | 振興局・観測開始日・所在地 |
| `area_map.json` | 上の 2 つから生成 | 地点 → area_code の対応 |

`amedastable.json` を正とするのは、**map JSON に出る地点と過不足なく
一致する**（1,286 地点で確認済み）ため。ame_master は振興局や観測開始日を
持つが更新が遅い。

## 手順

```bash
python watch_stations.py      # 取得して差分を表示。変更があれば終了コード 1
git diff stations/            # 何が変わったか読む
python build_area_map.py      # 変更があれば対応表を作り直す
git add stations/ && git commit -m "観測所: 〜"
```

**変更を読まずにコミットしないこと。** 新設地点は取得対象に加える必要があり、
廃止地点は 404 を出し続ける。`elems` が変われば観測要素が増減している。
