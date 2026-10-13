// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Yasuhiro / AIseed

import 'package:flet/flet.dart';
import 'package:flutter/widgets.dart';

import 'gpufield.dart';

class Extension extends FletExtension {
  @override
  Widget? createWidget(Key? key, Control control) {
    switch (control.type) {
      case "GpuField":
        return GpuFieldControl(key: key, control: control);
      default:
        return null;
    }
  }
}
