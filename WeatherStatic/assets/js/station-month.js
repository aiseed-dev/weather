/*
 * 地点ごとの、月を中心に前後 1 か月の最高・最低気温（旧サイトの /stations/jp/{地点}/{月}）を描く。
 *
 * ページは地点ごとに 1 枚（/stations/jp/{地点}/month/）で、旧 URL は _redirects の 200 で
 * URL のまま返される。月の数だけページを作ると Pages の上限（2 万ファイル）を超えるため。
 * ここで URL から月を読み、ページに埋め込んだ日別の観測値と平年値（generate.py の
 * build_stations）から SVG を描く。見た目は地点ページの 30 日のグラフ
 * （weatherlib/svgchart.py の timeseries_svg）に合わせる。
 *
 * 濃い線は「選んだ月の期間が始まっている一番新しい年」。1 本の線に別の年を混ぜない。
 */
(function () {
    "use strict";

    var dataEl = document.getElementById("month-data");
    var chart = document.getElementById("month-chart");
    if (!dataEl || !chart) return;
    var D = JSON.parse(dataEl.textContent);
    var DAY = 86400000;
    var PAST_YEARS = 7;
    var COLOR = { tmax: "#F92500", tmin: "#0C00CC" };
    var NORMAL = { tmax: "#d9573f", tmin: "#5a5fd6" };
    var PAST = { tmax: "rgba(249,37,0,.22)", tmin: "rgba(12,0,204,.20)" };
    var LABEL = { tmax: "最高", tmin: "最低" };
    var FONT = 'font-family="sans-serif"';
    var baseTitle = document.title;

    function utc(s) { var p = s.split("-"); return Date.UTC(+p[0], +p[1] - 1, +p[2]); }
    var first = utc(D.from), last = utc(D.to);

    function ymd(t) { var d = new Date(t); return [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()]; }
    function jpDate(t) { var a = ymd(t); return a[0] + "年" + a[1] + "月" + a[2] + "日"; }
    function md(t) { var a = ymd(t); return a[1] + "/" + a[2]; }
    function c10(v) { return (v / 10).toFixed(1); }

    function observed(name, t) {
        var i = Math.round((t - first) / DAY);
        var v = i >= 0 && i < D[name].length ? D[name][i] : null;
        return v === undefined ? null : v;
    }
    // 2/29 の平年値は無いので 2/28 で代える（平年値の線を途切れさせない）
    function normal(name, t) {
        var a = ymd(t), month = D.normal[name][a[1] - 1] || [];
        var v = month[Math.min(a[2], month.length) - 1];
        return v === undefined ? null : v;
    }
    // 月 m（1〜12）を中心にした、年 y の期間 [前月の 1 日, 翌月の末日]
    function span(m, y) { return [Date.UTC(y, m - 2, 1), Date.UTC(y, m + 1, 0)]; }
    // 同じ月日の、years 年前の日（2/29 のように無い日は null）
    function shift(t, years) {
        var a = ymd(t), s = Date.UTC(a[0] - years, a[1] - 1, a[2]);
        return ymd(s)[1] === a[1] ? s : null;
    }

    function monthFromUrl() {
        var m = /^\/stations\/jp\/[^\/]+\/(\d{1,2})\/?$/.exec(location.pathname.toLowerCase());
        var n = m ? +m[1] : 0;
        return n >= 1 && n <= 12 ? n : ymd(last)[1];
    }

    function build(m) {
        var y = ymd(last)[0];
        while (span(m, y)[0] > last) y -= 1;
        var s = span(m, y), days = [];
        for (var t = s[0]; t <= s[1]; t += DAY) days.push(t);
        var out = { m: m, year: y, start: s[0], end: s[1], days: days, recent: {}, normal: {}, past: [] };
        ["tmax", "tmin"].forEach(function (k) {
            out.recent[k] = days.map(function (t) { return t <= last ? observed(k, t) : null; });
            out.normal[k] = days.map(function (t) { return normal(k, t); });
        });
        for (var back = 1; back <= PAST_YEARS; back += 1) {
            var line = { back: back };
            ["tmax", "tmin"].forEach(function (k) {
                line[k] = days.map(function (t) {
                    var p = shift(t, back);
                    return p === null ? null : observed(k, p);
                });
            });
            if (line.tmax.some(isNum) || line.tmin.some(isNum)) out.past.push(line);
        }
        return out;
    }
    function isNum(v) { return v !== null && v !== undefined; }

    // 欠測で切れる線の区間ごとに [[x, y], …]
    function runs(xs, vals, y) {
        var out = [], cur = [];
        vals.forEach(function (v, i) {
            if (isNum(v)) cur.push([xs[i], y(v)]);
            else if (cur.length) { out.push(cur); cur = []; }
        });
        if (cur.length) out.push(cur);
        return out;
    }
    function pathOf(run) {
        return run.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
    }

    function svg(w) {
        var width = 720, height = 340, ml = 44, mr = 12, mt = 16, mb = 26;
        var pw = width - ml - mr, ph = height - mt - mb, n = w.days.length;
        var all = [];
        ["tmax", "tmin"].forEach(function (k) {
            all = all.concat(w.recent[k], w.normal[k]);
            w.past.forEach(function (p) { all = all.concat(p[k]); });
        });
        all = all.filter(isNum);
        if (!all.length) return "";
        var lo = Math.floor(Math.min.apply(null, all) / 50) * 5 - 5;
        var hi = Math.ceil(Math.max.apply(null, all) / 50) * 5 + 5;
        var step = hi - lo > 45 ? 10 : 5;
        function y(v) { return mt + ph * (hi - v / 10) / (hi - lo); }
        var xs = w.days.map(function (_, i) { return ml + pw * i / Math.max(n - 1, 1); });
        var e = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + " " + height +
                 '" style="max-width:' + width + 'px;width:100%;height:auto;background:#fff" role="img" aria-label="' +
                 document.getElementById("month-title").textContent + '">'];
        for (var t = Math.ceil(lo / step) * step; t <= hi; t += step) {
            var yy = y(t * 10).toFixed(1);
            e.push('<line x1="' + ml + '" y1="' + yy + '" x2="' + (ml + pw) + '" y2="' + yy + '" stroke="' +
                   (t === 0 ? "#999" : "#e8edf2") + '"/>');
            e.push('<text x="' + (ml - 5) + '" y="' + (+yy + 4) + '" text-anchor="end" font-size="10" fill="#666" ' +
                   FONT + ">" + t + "</text>");
        }
        w.days.forEach(function (d, i) {
            var day = ymd(d)[2];
            if (day !== 1 && day !== 15) return;
            e.push('<line x1="' + xs[i].toFixed(1) + '" y1="' + mt + '" x2="' + xs[i].toFixed(1) + '" y2="' + (mt + ph) +
                   '" stroke="' + (day === 1 ? "#c8d2dc" : "#f0f3f7") + '"/>');
            e.push('<text x="' + xs[i].toFixed(1) + '" y="' + (mt + ph + 14) + '" text-anchor="middle" font-size="10" fill="#666" ' +
                   FONT + ">" + md(d) + "</text>");
        });
        function line(vals, color, widthPx, extra) {
            runs(xs, vals, y).forEach(function (r) {
                e.push('<path d="' + pathOf(r) + '" fill="none" stroke="' + color + '" stroke-width="' + widthPx + '"' +
                       (extra || "") + "/>");
            });
        }
        ["tmax", "tmin"].forEach(function (k) {
            w.past.slice().reverse().forEach(function (p) { line(p[k], PAST[k], 1, ' class="past-years"'); });
        });
        ["tmax", "tmin"].forEach(function (k) { line(w.normal[k], NORMAL[k], 1.5, ' stroke-dasharray="5 4"'); });
        ["tmax", "tmin"].forEach(function (k) {
            line(w.recent[k], COLOR[k], 2);
            w.recent[k].forEach(function (v, i) {
                if (!isNum(v)) return;
                var nv = w.normal[k][i];
                e.push('<circle cx="' + xs[i].toFixed(1) + '" cy="' + y(v).toFixed(1) + '" r="2.2" fill="' + COLOR[k] + '"><title>' +
                       md(w.days[i]) + " " + LABEL[k] + " " + c10(v) + "℃" +
                       (isNum(nv) ? "（平年 " + c10(nv) + "℃）" : "") + "</title></circle>");
            });
        });
        e.push('<rect x="' + ml + '" y="' + mt + '" width="' + pw + '" height="' + ph + '" fill="none" stroke="#c8d2dc"/>');
        e.push('<text x="' + (ml - 30) + '" y="' + (mt + 10) + '" font-size="10" fill="#666" ' + FONT + ">℃</text>");
        e.push("</svg>");
        return e.join("");
    }

    // 選んだ月（中心の月）の平均と極値。平年値は月平年値（D.normal_month）
    function stats(w) {
        var rows = [], a = ymd(w.start), centre = Date.UTC(a[0], a[1], 1);
        var inMonth = w.days.map(function (t) { return ymd(t)[1] === ymd(centre)[1]; });
        function pick(k) { return w.recent[k].filter(function (v, i) { return inMonth[i] && isNum(v); }); }
        ["tmax", "tmin"].forEach(function (k) {
            var vals = pick(k);
            if (!vals.length) return;
            var mean = vals.reduce(function (s, v) { return s + v; }, 0) / vals.length;
            var nm = D.normal_month[k][w.m - 1];
            var diff = isNum(nm) ? (mean - nm) / 10 : null;
            rows.push("<tr><th>平均" + LABEL[k] + "気温</th><td class=\"wright\">" + (mean / 10).toFixed(1) + " ℃</td><td class=\"wright\">" +
                      (isNum(nm) ? c10(nm) + " ℃" : "-") + "</td><td class=\"wright\">" +
                      (diff === null ? "-" : (diff > 0 ? "+" : "") + diff.toFixed(1)) + "</td></tr>");
        });
        var hiV = null, hiT = null, loV = null, loT = null, n = 0;
        w.days.forEach(function (t, i) {
            if (!inMonth[i]) return;
            var x = w.recent.tmax[i], m = w.recent.tmin[i];
            if (isNum(x) || isNum(m)) n += 1;
            if (isNum(x) && (hiV === null || x > hiV)) { hiV = x; hiT = t; }
            if (isNum(m) && (loV === null || m < loV)) { loV = m; loT = t; }
        });
        if (hiV !== null) rows.push("<tr><th>最も高い最高気温</th><td class=\"wright\">" + c10(hiV) + " ℃</td><td colspan=\"2\">" + md(hiT) + "</td></tr>");
        if (loV !== null) rows.push("<tr><th>最も低い最低気温</th><td class=\"wright\">" + c10(loV) + " ℃</td><td colspan=\"2\">" + md(loT) + "</td></tr>");
        return { rows: rows, n: n, year: ymd(centre)[0] };
    }

    function render(m) {
        var w = build(m);
        var name = document.getElementById("month-title").dataset.name ||
                   document.getElementById("month-title").textContent.replace(/の最高気温.*$/, "");
        document.getElementById("month-title").dataset.name = name;
        document.getElementById("month-title").textContent = name + "の最高気温・最低気温の推移 " + m + "月";
        document.title = baseTitle.replace(/の最高気温・最低気温の推移.*$/, "") + "の最高気温・最低気温の推移 " + m + "月";
        var upto = w.end > last ? "（" + jpDate(last) + "まで）" : "";
        document.getElementById("month-period").textContent = jpDate(w.start) + "〜" + jpDate(w.end) + upto;
        document.getElementById("month-recent").textContent =
            "（" + ymd(w.start)[0] + (ymd(w.end)[0] !== ymd(w.start)[0] ? "〜" + ymd(w.end)[0] : "") + "年）";
        var past = w.past.map(function (p) { return p.back; });
        document.getElementById("month-past").textContent = past.length
            ? "（" + (ymd(w.start)[0] - past[past.length - 1]) + "〜" + (ymd(w.start)[0] - 1) + "年）" : "";
        chart.innerHTML = svg(w) || '<p style="margin:8px">この期間の観測値も平年値もありません。</p>';
        var st = stats(w), table = document.getElementById("month-stats");
        table.tBodies[0].innerHTML = st.rows.join("");
        table.hidden = !st.rows.length;
        if (st.rows.length) {
            table.tHead.rows[0].cells[0].textContent = st.year + "年" + m + "月（" + st.n + "日分）";
        }
        document.querySelectorAll(".month-nav a").forEach(function (a) {
            if (+a.dataset.month === m) a.setAttribute("aria-current", "page");
            else a.removeAttribute("aria-current");
        });
    }

    document.querySelectorAll(".month-nav a").forEach(function (a) {
        a.addEventListener("click", function (ev) {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
            ev.preventDefault();
            history.pushState(null, "", a.getAttribute("href"));
            render(+a.dataset.month);
        });
    });
    window.addEventListener("popstate", function () { render(monthFromUrl()); });
    render(monthFromUrl());
})();
