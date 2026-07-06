/**
 * 服装投票ビーコン (Cloudflare Workers + KV)。
 *
 * Pages はアクセスログを提供しないため、vote.gif 相当をこの Worker が受ける。
 *   GET /vote.gif?d=YYYY-MM-DD&c=<地点コード>&v=<0|1|2>
 * KV キー v:{d}:{c}:{sha1(IP)} に v を保存（同一人の再投票は同一キーで上書き
 * = 重複排除）。TTL 14 日。集計は aggregate_votes.py --kv が日次で取り込む。
 *
 * デプロイ（ユーザー実行）:
 *   1. ダッシュボード → Workers & Pages → KV → namespace "weather-votes" 作成
 *   2. Worker 作成 → このファイルを貼り付け → Settings → Bindings で
 *      KV namespace を VOTES として bind
 *   3. サイト生成時に WEATHER_VOTE_URL=https://<worker>.workers.dev/vote.gif
 */

const GIF = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAAAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="), c => c.charCodeAt(0));

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const d = url.searchParams.get("d") || "";
    const c = url.searchParams.get("c") || "";
    const v = url.searchParams.get("v") || "";
    const ok = /^\d{4}-\d{2}-\d{2}$/.test(d) && /^\d{1,6}$/.test(c) && /^[0-2]$/.test(v);
    if (ok && env.VOTES) {
      const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
      const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(ip));
      const hash = [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
      await env.VOTES.put(`v:${d}:${c}:${hash}`, v, { expirationTtl: 14 * 86400 });
    }
    return new Response(GIF, {
      headers: {
        "Content-Type": "image/gif",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
