/*
 * アプリの案内ページ（/app/・/app/forecast/）の小さな動き。
 *
 * - 入れ方のコマンドに「写す」ボタンを付ける
 * - スマートフォンの人が、このページを自分に送れる（navigator.share、無ければアドレスを写す）
 * - GitHub などへのリンクを押した・コマンドを写した・ページを送ったことを、自前のアクセス解析
 *   （kaiseki）の出来事にする。名前は kaiseki の毎日のまとめが見出し付きで数える out と use。
 *   アプリを入れる人が増えたかを、ページを見た数ではなく、この出来事で見るため
 */
(function () {
    "use strict";

    function event(name, value) {
        if (window.kaiseki && window.kaiseki.event) window.kaiseki.event(name, { value: value });
    }
    function copyByTextarea(text) {
        var t = document.createElement("textarea");
        t.value = text;
        document.body.appendChild(t);
        t.select();
        try { return document.execCommand("copy"); } finally { document.body.removeChild(t); }
    }
    // 押した直後に写す。navigator.clipboard は許可を待ったまま返らないことがある（2026-10-16、
    // アプリ内のブラウザ）ので、写せなかったときだけ使う
    function copyText(text) {
        var ok = false;
        try { ok = copyByTextarea(text); } catch (e) { ok = false; }
        if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text);
        }
        return ok ? Promise.resolve() : Promise.reject(new Error("copy"));
    }

    document.querySelectorAll(".app-code").forEach(function (box, i) {
        var pre = box.querySelector("pre");
        if (!pre) return;
        var b = document.createElement("button");
        b.type = "button";
        b.className = "app-copy";
        b.textContent = "写す";
        b.addEventListener("click", function () {
            copyText(pre.textContent).then(function () {
                b.textContent = "写しました";
                setTimeout(function () { b.textContent = "写す"; }, 2000);
            });
            event("use", "install_cmd_" + (i + 1));
        });
        box.appendChild(b);
    });

    document.querySelectorAll("[data-share-page]").forEach(function (b) {
        b.addEventListener("click", function () {
            var url = location.origin + location.pathname;
            if (navigator.share) {
                navigator.share({ title: document.title, url: url }).catch(function () {});
                event("use", "share_page");
            } else {
                copyText(url).then(function () {
                    var done = b.parentNode.querySelector(".app-share-done");
                    if (done) done.hidden = false;
                });
                event("use", "copy_page_url");
            }
        });
    });

    document.addEventListener("click", function (e) {
        var a = e.target.closest && e.target.closest("a[href]");
        if (!a || a.host === location.host) return;
        event("out", (a.host + a.pathname).slice(0, 100));
    });
})();
