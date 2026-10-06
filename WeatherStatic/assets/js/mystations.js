/*
 * 自分の地点（実況のページの上のカード）。
 *
 * 選んだ地点（アメダス番号、最大 10）はこのブラウザーの localStorage にだけ保存し、どこにも送らない。
 * 値は /data/amedas-latest.json（generate_status.py の build_latest_json。10 分ごと）から読み、
 * ページを開いたままでも 10 分ごとに読み直す。
 *
 * ページ側の置き場所:
 *   #mine                 … カードと「地点を足す」欄（templates/status/_mine.html）
 *   [data-a="番号"]        … 地図の点・表の行。自分の地点なら class "mine" を付けて目立たせる
 *   [data-mine-toggle="番号"] … 地点のページの「自分の地点に入れる」ボタン
 */
(function () {
    "use strict";
    var KEY = "my-stations-v1", MAX = 10, DATA = "/data/amedas-latest.json", EVERY = 10 * 60 * 1000;
    var WDIR = ["静穏", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南",
                "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"];
    var latest = null;

    function load() {
        try {
            var v = JSON.parse(localStorage.getItem(KEY) || "[]");
            return Array.isArray(v) ? v.filter(function (x) { return typeof x === "string"; }).slice(0, MAX) : [];
        } catch (e) { return []; }
    }
    function save(list) {
        try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX))); } catch (e) {}
        mark();
    }
    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function t10(v) { return v == null ? null : (v / 10).toFixed(1); }

    function fetchLatest() {
        return fetch(DATA, { cache: "no-store" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .catch(function () { return null; })
            .then(function (d) { if (d && d.stations) latest = d; return latest; });
    }

    // 地図の点・表の行の目印
    function mark() {
        var set = {};
        load().forEach(function (a) { set[a] = 1; });
        document.querySelectorAll("[data-a]").forEach(function (el) {
            el.classList.toggle("mine", !!set[el.getAttribute("data-a")]);
        });
        document.querySelectorAll("[data-mine-toggle]").forEach(function (b) {
            var on = !!set[b.getAttribute("data-mine-toggle")];
            b.setAttribute("aria-pressed", on ? "true" : "false");
            b.textContent = on ? "★ 自分の地点に入っています（外す）" : "☆ 自分の地点に入れる";
        });
    }

    function card(a, i, n) {
        var s = latest && latest.stations[a];
        if (!s) {
            return '<div class="mine-card" data-mine="' + esc(a) + '"><div class="mine-head"><b>番号 ' + esc(a) +
                   '</b><button type="button" class="mine-x" aria-label="外す">×</button></div>' +
                   '<div class="mine-sub">この地点の値はいまありません</div></div>';
        }
        var temp = t10(s[3]), lines = [];
        if (s[4] != null || s[6] != null) {
            lines.push("最高 " + (s[4] != null ? t10(s[4]) + "℃" + (s[5] ? "（" + esc(s[5]) + "）" : "") : "-") +
                       "・最低 " + (s[6] != null ? t10(s[6]) + "℃" + (s[7] ? "（" + esc(s[7]) + "）" : "") : "-"));
        }
        var w = [];
        if (s[9] != null) w.push((s[8] ? WDIR[s[8]] + " " : "") + t10(s[9]) + " m/s");
        if (s[10] != null) w.push("1時間 " + t10(s[10]) + " mm");
        if (s[11] != null) w.push("24時間 " + t10(s[11]) + " mm");
        if (s[12] != null && s[12] > 0) w.push("積雪 " + s[12] + " cm");
        if (w.length) lines.push(w.join("・"));
        return '<div class="mine-card" draggable="true" data-mine="' + esc(a) + '">' +
               '<div class="mine-head"><a href="' + esc(s[2]) + '"><b>' + esc(s[0]) + '</b><small>' + esc(s[1]) + '</small></a>' +
               (i > 0 ? '<button type="button" class="mine-left" aria-label="前へ">←</button>' : "") +
               '<button type="button" class="mine-x" aria-label="外す">×</button></div>' +
               '<div class="mine-t">' + (temp != null ? temp + "<small>℃</small>" : '<span class="mine-none">気温の観測なし</span>') + "</div>" +
               lines.map(function (l) { return '<div class="mine-sub">' + l + "</div>"; }).join("") + "</div>";
    }

    function render() {
        var box = document.getElementById("mine");
        if (!box) return;
        var list = load(), cards = box.querySelector(".mine-cards"), time = box.querySelector(".mine-time");
        cards.innerHTML = list.map(card).join("");
        box.classList.toggle("mine-empty", !list.length);
        if (time) time.textContent = latest ? latest.time.slice(11) + " 現在" : "";
        cards.querySelectorAll(".mine-x").forEach(function (b) {
            b.onclick = function () {
                var a = b.closest(".mine-card").getAttribute("data-mine");
                save(load().filter(function (x) { return x !== a; }));
                render();
            };
        });
        cards.querySelectorAll(".mine-left").forEach(function (b) {
            b.onclick = function () {
                var a = b.closest(".mine-card").getAttribute("data-mine"), l = load(), i = l.indexOf(a);
                if (i > 0) { l.splice(i, 1); l.splice(i - 1, 0, a); save(l); render(); }
            };
        });
        // ドラッグで並べ替え（マウス）。指では「←」で前へ
        var dragged = null;
        cards.querySelectorAll(".mine-card[draggable]").forEach(function (c) {
            c.addEventListener("dragstart", function () { dragged = c.getAttribute("data-mine"); c.classList.add("dragging"); });
            c.addEventListener("dragend", function () { c.classList.remove("dragging"); });
            c.addEventListener("dragover", function (e) { e.preventDefault(); });
            c.addEventListener("drop", function (e) {
                e.preventDefault();
                var to = c.getAttribute("data-mine"), l = load();
                if (!dragged || dragged === to) return;
                l.splice(l.indexOf(dragged), 1);
                l.splice(l.indexOf(to), 0, dragged);
                save(l); render();
            });
        });
    }

    function setupAdd() {
        var box = document.getElementById("mine");
        if (!box || !latest) return;
        var input = box.querySelector(".mine-input"), list = box.querySelector("datalist"), msg = box.querySelector(".mine-msg");
        var byLabel = {};
        var opts = Object.keys(latest.stations).map(function (a) {
            var s = latest.stations[a], label = s[0] + "（" + s[1] + "）";
            byLabel[label] = a;
            return label;
        }).sort();
        list.innerHTML = opts.map(function (l) { return '<option value="' + esc(l) + '">'; }).join("");
        function add() {
            var v = input.value.trim(), a = byLabel[v];
            if (!a) {   // 名前だけでも、ひとつに決まれば足す
                var hit = opts.filter(function (l) { return l.indexOf(v) === 0; });
                if (v && hit.length === 1) a = byLabel[hit[0]];
            }
            if (!a) { msg.textContent = v ? "候補から選んでください" : ""; return; }
            var l = load();
            if (l.indexOf(a) >= 0) { msg.textContent = "もう入っています"; return; }
            if (l.length >= MAX) { msg.textContent = MAX + " 地点まで。どれかを外してから足してください"; return; }
            l.push(a); save(l); input.value = ""; msg.textContent = ""; render();
        }
        input.addEventListener("change", add);
        box.querySelector(".mine-add").addEventListener("submit", function (e) { e.preventDefault(); add(); });
    }

    function setupToggles() {
        document.querySelectorAll("[data-mine-toggle]").forEach(function (b) {
            b.addEventListener("click", function () {
                var a = b.getAttribute("data-mine-toggle"), l = load(), i = l.indexOf(a);
                if (i >= 0) l.splice(i, 1);
                else if (l.length >= MAX) { alert(MAX + " 地点まで入れられます。実況のページでどれかを外してください"); return; }
                else l.push(a);
                save(l);
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        mark();
        setupToggles();
        if (!document.getElementById("mine")) return;
        render();
        fetchLatest().then(function () { setupAdd(); render(); });
        setInterval(function () {
            if (document.visibilityState === "visible") fetchLatest().then(render);
        }, EVERY);
    });
})();
