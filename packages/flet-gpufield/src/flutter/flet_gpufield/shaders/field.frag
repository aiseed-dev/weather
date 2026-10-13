// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Yasuhiro / AIseed
//
// 等緯度経度の格子を、画面の画素ごとに投影を逆にたどって経緯度を求め、双一次補間して配色する。
//
// 格子の値は 16 bit に詰めて RG に入れてある（R が上位、G が下位。A<0.5 は欠測）。
// 画像の補間（filterQuality）は使わない。16 bit を 2 つの 8 bit に分けているので、
// 上位と下位を別々に補間すると繰り上がりが壊れる。格子点の中心を 4 点読んで自分で補間する。
// 配色表は透明度も持つ（降水の「降っていない」所は地色が透ける）。
// 値が階級（レーダーの降水強度など）のときは補間せず、最寄りの格子点の値で塗る
// （階級の間を補間すると、観測されていない強さの縁ができる）。
#version 460 core
#include <flutter/runtime_effect.glsl>

precision highp float;

uniform vec2 uSize;      // 描く範囲（論理画素）
uniform vec4 uView;      // 正距円筒: 左端の経度、上端の緯度、1 画素あたりの度、未使用
                         // 極:       極の画面位置 x・y、1 画素あたりの度（余緯度）、真下に来る経度
uniform vec4 uGrid;      // 最初の格子点の経度・緯度、経度・緯度の間隔
uniform vec2 uGridN;     // 格子の列数・行数
uniform vec4 uMode;      // 投影（0 正距円筒・1 北極・2 南極）、経度方向につながるか、地色の有無、
                         // 最寄りの格子点で塗るか
uniform sampler2D uField;
uniform sampler2D uLut;  // 256 × 1、RGBA
uniform sampler2D uBase; // 陸海（R が陸の割合）

out vec4 fragColor;

const vec3 SEA = vec3(0.345, 0.361, 0.392);   // #585c64（chart-base-design）
const vec3 LAND = vec3(0.463, 0.478, 0.502);  // #767a80
const vec3 OUTSIDE = vec3(0.10, 0.11, 0.13);

float cell(float i, float j) {
    vec4 c = texture(uField, (vec2(i, j) + 0.5) / uGridN);
    if (c.a < 0.5) {
        return -1.0;
    }
    return (floor(c.r * 255.0 + 0.5) * 256.0 + floor(c.g * 255.0 + 0.5)) / 65535.0;
}

void main() {
    vec2 p = FlutterFragCoord().xy;
    float lon;
    float lat;
    if (uMode.x < 0.5) {
        lon = uView.x + p.x * uView.z;
        lat = uView.y - p.y * uView.z;
    } else {
        vec2 d = p - uView.xy;
        float r = length(d) * uView.z;
        if (r > 180.0) {
            fragColor = vec4(OUTSIDE, 1.0);
            return;
        }
        float ang = degrees(atan(d.x, d.y));
        if (uMode.x < 1.5) {
            lat = 90.0 - r;
            lon = uView.w + ang;
        } else {
            lat = -90.0 + r;
            lon = uView.w - ang;
        }
    }
    bool periodic = uMode.y > 0.5;
    float fi = periodic ? mod(lon - uGrid.x, 360.0) / uGrid.z : (lon - uGrid.x) / uGrid.z;
    float fj = (uGrid.y - lat) / uGrid.w;
    float maxI = periodic ? uGridN.x : uGridN.x - 1.0;
    if (fj < 0.0 || fj > uGridN.y - 1.0 || fi < 0.0 || fi > maxI) {
        fragColor = vec4(OUTSIDE, 1.0);
        return;
    }
    vec3 base = SEA;
    if (uMode.z > 0.5) {
        // 陸海は画像の双一次補間で読む（0/1 の境が滑らかになり、海岸がぎざぎざにならない）
        float land = texture(uBase, vec2((fi + 0.5) / uGridN.x, (fj + 0.5) / uGridN.y)).r;
        base = mix(SEA, LAND, land);
    }

    if (uMode.w > 0.5) {
        float ni = floor(fi + 0.5);
        ni = periodic ? mod(ni, uGridN.x) : min(ni, uGridN.x - 1.0);
        float v = cell(ni, min(floor(fj + 0.5), uGridN.y - 1.0));
        if (v < 0.0) {
            fragColor = vec4(base, 1.0);
            return;
        }
        vec4 c1 = texture(uLut, vec2(clamp(v, 0.0, 1.0) * (255.0 / 256.0) + 0.5 / 256.0, 0.5));
        fragColor = vec4(mix(base, c1.rgb, c1.a), 1.0);
        return;
    }

    float j0 = min(floor(fj), uGridN.y - 2.0);
    float i0 = periodic ? floor(fi) : min(floor(fi), uGridN.x - 2.0);
    float i1 = periodic ? mod(i0 + 1.0, uGridN.x) : i0 + 1.0;
    i0 = periodic ? mod(i0, uGridN.x) : i0;
    float fx = fi - floor(fi);
    if (!periodic && floor(fi) > uGridN.x - 2.0) {
        fx = fi - i0;
    }
    float fy = fj - j0;

    float a = cell(i0, j0);
    float b = cell(i1, j0);
    float c = cell(i0, j0 + 1.0);
    float d = cell(i1, j0 + 1.0);
    if (a < 0.0 || b < 0.0 || c < 0.0 || d < 0.0) {
        fragColor = vec4(base, 1.0);
        return;
    }
    float q = mix(mix(a, b, fx), mix(c, d, fx), fy);
    vec4 col = texture(uLut, vec2(clamp(q, 0.0, 1.0) * (255.0 / 256.0) + 0.5 / 256.0, 0.5));
    fragColor = vec4(mix(base, col.rgb, col.a), 1.0);
}
