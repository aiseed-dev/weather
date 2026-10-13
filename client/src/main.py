# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""実行環境だけを作るためのアプリ（中身は使わない）。

`flet build linux` は Flutter 側（実行環境）と Python 側をまとめて作る。アプリ本体の依存
（cfgrib・eccodes など）を Flet の組み込み Python に入れるのは重いので、ここでは部品
（flet-gpufield）だけを入れた小さなアプリで実行環境を作り、プロジェクト直下の build/linux に置く。
`flet run` はその実行環境を見つけて使い、アプリ本体は今までどおり conda の Python で動く。
"""
import flet as ft


def main(page: ft.Page):
    page.add(ft.Text("AIseed Weather の実行環境です。プロジェクトのフォルダーで flet run を使ってください。"))


ft.run(main)
