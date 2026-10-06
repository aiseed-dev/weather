/*
 * 過去の記録のページ（旧サイトの /temperature/summerday/…・/temperature/winterday/… など）を描く。
 *
 * ページは種類ごとに 1 枚の枠（/history/day/ など）で、_redirects の 200 で旧 URL の
 * まま返される。ここで URL を読み、月ごとのデータ（/data/history/。生成は
 * weatherlib/history.py の export）を取ってきて表を描く。集計は生成側で済んでおり、
 * ここでは順位を付けて並べるだけ（同じ値は同じ順位）。
 */
(function () {
    "use strict";

    // データの種類の記号（weatherlib/history.py の KINDS）。冬は URL の記号に w を付ける
    var KINDS = {
        a: "猛暑日（最高気温が35℃以上）",
        b: "真夏日（最高気温が30℃以上）",
        c: "平均気温が30℃以上",
        d: "最低気温が25℃以上",
        wa: "冬日（最低気温が0℃未満）",
        wb: "平均気温が0℃未満",
        wc: "真冬日（最高気温が0℃未満）"
    };
    var KIND_SHORT = { a: "猛暑日", b: "真夏日", c: "平均気温が30℃以上", d: "最低気温が25℃以上" };
    var INTRO = {
        summer: "2010 年からの猛暑日（最高気温が35℃以上）、真夏日（最高気温が30℃以上）、平均気温が30℃以上、" +
                "最低気温が25℃以上となった地点の数を、日ごとに集計しています。",
        winter: "2010 年からの冬日（最低気温が0℃未満）、平均気温が0℃未満、真冬日（最高気温が0℃未満）と" +
                "なった地点の数を、日ごとに集計しています。"
    };
    // 冬の月の表は 2 枚: 冬日と平均気温 0℃未満（/temperature/wintermonth/{月}）、真冬日（…/wintermonth1/{月}）
    function winterMonthUrl(k, mo) { return "/temperature/wintermonth" + (k === "c" ? "1" : "") + "/" + mo; }

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function pad2(n) { return (n < 10 ? "0" : "") + n; }
    function getJSON(url) {
        return fetch(url).then(function (r) { return r.ok ? r.json() : null; })
                         .catch(function () { return null; });
    }
    function root() { return document.getElementById("history"); }
    function show(html) { root().innerHTML = html; }
    function notFound(msg) {
        var m = document.createElement("meta");
        m.name = "robots"; m.content = "noindex";
        document.head.appendChild(m);
        show("<p>" + esc(msg) + "</p>");
    }
    function setTitle(t) {
        document.title = t;
        var h = document.getElementById("history-title");
        if (h) h.textContent = t;
    }

    // [[地点, 値], …]（並び済み）に順位を付ける。同じ値は同じ順位（1, 2, 2, 4 …）
    function ranks(rows, valueAt) {
        var out = [], prev = null, rank = 0;
        rows.forEach(function (r, i) {
            var v = r[valueAt];
            if (v !== prev) { rank = i + 1; prev = v; }
            out.push(rank);
        });
        return out;
    }
    function rankCell(rank) {
        return rank <= 3 ? '<span class="rank-badge r' + rank + '">' + rank + "</span>" : String(rank);
    }
    function stationCell(st, code) {
        var s = st[code] || [String(code), "", null];
        var name = s[2] ? '<a href="/stations/jp/' + esc(s[2]) + '/">' + esc(s[0]) + "</a>" : esc(s[0]);
        return esc(s[1]) + name;
    }
    function fmt10(v) { return (v / 10).toFixed(1); }

    function table(title, head, rows) {
        return '<div class="item"><h5>' + esc(title) + '</h5><table class="wtable"><tbody>' +
               "<tr>" + head.map(function (h) { return "<th>" + h + "</th>"; }).join("") + "</tr>" +
               rows.join("") + "</tbody></table></div>";
    }

    // ---------------------------------------------------------------- その日の地点
    function day() {
        var m = /^\/temperature\/(summer|winter)day\/([abcd])(\d{4})(\d{2})(\d{2})\/?$/.exec(location.pathname.toLowerCase());
        if (!m || (m[1] === "winter" && m[2] === "d"))
            return notFound("URL の形が正しくありません（例: /temperature/summerday/a20180723、/temperature/winterday/c20180125）。");
        var winter = m[1] === "winter", k = m[2], kind = (winter ? "w" : "") + k;
        var y = +m[3], mo = +m[4], d = +m[5];
        var dt = new Date(Date.UTC(y, mo - 1, d));
        if (dt.getUTCMonth() !== mo - 1) return notFound("その日付はありません。");
        var title = y + "年" + mo + "月" + d + "日の" + KINDS[kind] + "の地点";
        setTitle(title);
        function dayUrl(t) {
            return "/temperature/" + m[1] + "day/" + k + t.getUTCFullYear() + pad2(t.getUTCMonth() + 1) + pad2(t.getUTCDate());
        }
        var nav = '<p class="hist-nav"><a href="' + dayUrl(new Date(dt - 864e5)) + '">← 前の日</a>' +
                  '<a href="' + dayUrl(new Date(+dt + 864e5)) + '">次の日 →</a>' +
                  '<a href="' + (winter ? winterMonthUrl(k, mo) : "/temperature/summermonth/" + k + "/" + mo) +
                  '">月別の一覧に戻る</a></p>';
        var order = winter ? "気温の低い順" : "気温の高い順";
        Promise.all([getJSON("/data/history/" + y + "/" + pad2(mo) + ".json"),
                     getJSON("/data/history/stations.json")]).then(function (res) {
            var month = res[0], st = (res[1] || {}).stations || {};
            var e = month && month.days[String(d)] && month.days[String(d)][kind];
            if (!e) {
                show(nav + "<p>この日の" + esc(KINDS[kind]) + "のデータはありません。</p>");
                return;
            }
            var rv = ranks(e.v, 1), rr = ranks(e.r, 1);
            var html = "<p>" + y + "年" + mo + "月" + d + "日に" + esc(KINDS[kind]) +
                       "となった地点を、" + order + "と連続日数の多い順にランキングしています（" +
                       e.v.length + " 地点）。</p>" + nav + document.getElementById("history-note").innerHTML;
            if (!e.v.length) {
                show(html + "<p>この日に" + esc(KINDS[kind]) + "となった地点はありません。</p>");
                return;
            }
            html += '<div class="rank-grid">' + table(order, ["順位", "地点", "気温（℃）"],
                e.v.map(function (r, i) {
                    return '<tr><td class="wright">' + rankCell(rv[i]) + "</td><td>" + stationCell(st, r[0]) +
                           '</td><td class="wright">' + fmt10(r[1]) + (r[2] ? " ]" : "") + "</td></tr>";
                }));
            if (e.r.length) {
                html += table("連続日数の多い順", ["順位", "地点", "日数"], e.r.map(function (r, i) {
                    return '<tr><td class="wright">' + rr[i] + "</td><td>" + stationCell(st, r[0]) +
                           '</td><td class="wright">' + r[1] + "</td></tr>";
                }));
            }
            show(html + "</div>");
        });
    }

    // ---------------------------------------------------------------- 月の日ごとの地点数
    // 夏: /temperature/summermonth/{a|b|c|d}/{月}（1 枚に 1 種類）
    // 冬: /temperature/wintermonth/{月}（冬日と平均気温 0℃未満の 2 表）、/temperature/wintermonth1/{月}（真冬日）
    function month() {
        var path = location.pathname.toLowerCase();
        var ms = /^\/temperature\/summermonth\/([abcd])\/(\d{1,2})\/?$/.exec(path);
        var mw = /^\/temperature\/wintermonth(1?)\/(\d{1,2})\/?$/.exec(path);
        var m = ms || mw;
        if (!m || +m[2] < 1 || +m[2] > 12)
            return notFound("URL の形が正しくありません（例: /temperature/summermonth/a/7、/temperature/wintermonth/1）。");
        var mo = +m[2], winter = !!mw, tabs, monthsNav, kinds, title;
        if (winter) {
            var mafuyu = m[1] === "1";
            kinds = mafuyu ? ["wc"] : ["wa", "wb"];
            title = mo + "月の" + (mafuyu ? "真冬日" : "冬日・平均気温が0℃未満") + "の日別の地点数";
            tabs = [["a", "冬日・平均気温が0℃未満"], ["c", "真冬日"]].map(function (t) {
                var on = (t[0] === "c") === mafuyu;
                return '<a href="' + winterMonthUrl(t[0], mo) + '">' + (on ? "<b>" + t[1] + "</b>" : t[1]) + "</a>";
            }).join("");
            monthsNav = [10, 11, 12, 1, 2, 3, 4, 5].map(function (mm) {
                var u = winterMonthUrl(mafuyu ? "c" : "a", mm);
                return '<a href="' + u + '">' + (mm === mo ? "<b>" + mm + "月</b>" : mm + "月") + "</a>";
            }).join("");
        } else {
            var kind = m[1];
            kinds = [kind];
            title = mo + "月の" + KINDS[kind] + "の日別の地点数";
            tabs = Object.keys(KIND_SHORT).map(function (k) {
                var label = KIND_SHORT[k];
                return '<a href="/temperature/summermonth/' + k + "/" + mo + '">' + (k === kind ? "<b>" + label + "</b>" : label) + "</a>";
            }).join("");
            monthsNav = [5, 6, 7, 8, 9, 10].map(function (mm) {
                return '<a href="/temperature/summermonth/' + kind + "/" + mm + '">' + (mm === mo ? "<b>" + mm + "月</b>" : mm + "月") + "</a>";
            }).join("");
        }
        setTitle(title);
        function grid(kind, t) {
            var k = kind.slice(-1), base = winter ? "/temperature/winterday/" : "/temperature/summerday/";
            var rows = ["<tr><th>日</th>" + t.years.map(function (y) { return "<th>" + y + "</th>"; }).join("") + "</tr>"];
            t.grid.forEach(function (row, di) {
                var dd = di + 1;
                rows.push("<tr><td>" + dd + "</td>" + row.map(function (n, yi) {
                    if (n === null) return "<td></td>";
                    if (n === 0) return '<td class="zero">0</td>';
                    return '<td><a href="' + base + k + t.years[yi] + pad2(mo) + pad2(dd) + '">' + n + "</a></td>";
                }).join("") + "</tr>");
            });
            return (kinds.length > 1 ? "<h4>" + esc(KINDS[kind]) + "</h4>" : "") +
                   '<div class="mwrap"><table class="mtable"><tbody>' + rows.join("") + "</tbody></table></div>";
        }
        Promise.all(kinds.map(function (kind) { return getJSON("/data/history/table/" + kind + mo + ".json"); }))
            .then(function (tables) {
                var html = "<p>" + INTRO[winter ? "winter" : "summer"] + "</p>" +
                           '<p class="hist-nav">' + tabs + '</p><p class="hist-nav">' + monthsNav + "</p>" +
                           document.getElementById("history-note").innerHTML;
                if (!tables.some(Boolean)) { show(html + "<p>データがありません。</p>"); return; }
                show(html + kinds.map(function (kind, i) {
                    return tables[i] ? grid(kind, tables[i]) : "";
                }).join(""));
            });
    }

    // ---------------------------------------------------------------- 月の平均気温のランキング
    function monthly() {
        var m = /^\/monthly\/(monthly|monthlyl)\/(\d{4})(\d{2})\/?$/.exec(location.pathname.toLowerCase());
        if (!m || +m[3] < 1 || +m[3] > 12) return notFound("URL の形が正しくありません（例: /monthly/monthly/201807）。");
        var high = m[1] === "monthly", y = +m[2], mo = +m[3];
        var base = high ? "monthly" : "monthlyl";
        setTitle(y + "年" + mo + "月の" + (high ? "気温" : "低気温") + "のランキング");
        var prev = mo === 1 ? [y - 1, 12] : [y, mo - 1], next = mo === 12 ? [y + 1, 1] : [y, mo + 1];
        var nav = '<p class="hist-nav"><a href="/monthly/' + base + "/" + prev[0] + pad2(prev[1]) + '">← 前の月</a>' +
                  '<a href="/monthly/' + base + "/" + next[0] + pad2(next[1]) + '">次の月 →</a>' +
                  '<a href="/monthly/' + (high ? "monthlyl" : "monthly") + "/" + y + pad2(mo) + '">' + (high ? "低い順" : "高い順") + "</a></p>";
        var titles = ["日最高気温の平均", "日平均気温の平均", "日最低気温の平均"];
        if (!high) titles = titles.slice().reverse();
        Promise.all([getJSON("/data/history/" + y + "/monthly.json"),
                     getJSON("/data/history/stations.json")]).then(function (res) {
            var mm = res[0] && res[0].months[String(mo)], st = (res[1] || {}).stations || {};
            var tables = mm && (high ? mm.high : mm.low);
            if (!tables || !tables.length) { show(nav + "<p>この月のデータはありません。</p>"); return; }
            var f = mm.first.split("-"), l = mm.last.split("-");
            var html = "<p>気象庁の観測所のうち気温を測定している所を対象に、" + y + "年" + mo +
                       "月の日最高気温、日平均気温、日最低気温それぞれの平均値の" + (high ? "高い順" : "低い順") +
                       "に上位100位までをリストにしています。</p><p>※" + (+f[0]) + " 年 " + (+f[1]) + " 月 " + (+f[2]) +
                       " 日から " + (+l[0]) + " 年 " + (+l[1]) + " 月 " + (+l[2]) + " 日までの記録を集計したものです。</p>" +
                       nav + document.getElementById("history-note").innerHTML + '<div class="rank-grid">';
            tables.forEach(function (rows, ti) {
                var rk = ranks(rows, 1);
                html += table(titles[ti], ["順位", "地点", "気温（℃）"], rows.map(function (r, i) {
                    return '<tr><td class="wright">' + rankCell(rk[i]) + "</td><td>" + stationCell(st, r[0]) +
                           '</td><td class="wright">' + fmt10(r[1]) + "</td></tr>";
                }));
            });
            show(html + "</div>");
        });
    }

    window.HISTORY = { day: day, month: month, monthly: monthly };
})();
