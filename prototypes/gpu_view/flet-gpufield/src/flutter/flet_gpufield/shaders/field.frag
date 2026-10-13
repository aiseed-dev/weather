// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Yasuhiro / AIseed
//
// 正距円筒の等間隔格子を、画面の画素ごとに双一次補間して配色する。
// 格子の値は 16 bit に詰めて RG に入れてある（R が上位、G が下位。A=0 は欠測）。
// 画像の補間（filterQuality）は使わない。16 bit を 2 つの 8 bit に分けているので、
// 上位と下位を別々に補間すると繰り上がりが壊れる。格子点の中心を 4 点読んで自分で補間する。
#version 460 core
#include <flutter/runtime_effect.glsl>

precision highp float;

uniform vec2 uSize;      // 描く範囲（論理画素）
uniform vec4 uView;      // 左端の経度、上端の緯度、1 画素あたりの度、未使用
uniform vec4 uGrid;      // 最初の格子点の経度・緯度、経度・緯度の間隔
uniform vec2 uGridN;     // 格子の列数・行数
uniform sampler2D uField;
uniform sampler2D uLut;  // 256 × 1

out vec4 fragColor;

const vec4 BACKGROUND = vec4(0.13, 0.14, 0.16, 1.0);

// 格子点 ij（整数）の値（0〜1）。欠測なら -1
float cell(vec2 ij) {
    vec4 c = texture(uField, (ij + 0.5) / uGridN);
    if (c.a < 0.5) {
        return -1.0;
    }
    return (floor(c.r * 255.0 + 0.5) * 256.0 + floor(c.g * 255.0 + 0.5)) / 65535.0;
}

void main() {
    vec2 p = FlutterFragCoord().xy;
    float lon = uView.x + p.x * uView.z;
    float lat = uView.y - p.y * uView.z;
    float fi = (lon - uGrid.x) / uGrid.z;
    float fj = (uGrid.y - lat) / uGrid.w;
    if (fi < 0.0 || fj < 0.0 || fi > uGridN.x - 1.0 || fj > uGridN.y - 1.0) {
        fragColor = BACKGROUND;
        return;
    }
    vec2 i0 = min(floor(vec2(fi, fj)), uGridN - 2.0);
    vec2 f = vec2(fi, fj) - i0;
    float a = cell(i0);
    float b = cell(i0 + vec2(1.0, 0.0));
    float c = cell(i0 + vec2(0.0, 1.0));
    float d = cell(i0 + vec2(1.0, 1.0));
    if (a < 0.0 || b < 0.0 || c < 0.0 || d < 0.0) {
        fragColor = BACKGROUND;
        return;
    }
    float q = mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
    // 配色表の 256 段の中心を読む
    vec3 rgb = texture(uLut, vec2(clamp(q, 0.0, 1.0) * (255.0 / 256.0) + 0.5 / 256.0, 0.5)).rgb;
    fragColor = vec4(rgb, 1.0);
}
