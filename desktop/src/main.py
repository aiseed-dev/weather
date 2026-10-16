# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""配布用のアプリの入口。

本体は aiseed_weather.main で、読み込んだときにログの設定をし、`__main__` として動かされたときに
ウィンドウを開く。flet build はこのフォルダー（src）をアプリとして抱えるので、本体を
`__main__` として走らせる。
"""
import runpy

runpy.run_module("aiseed_weather.main", run_name="__main__")
