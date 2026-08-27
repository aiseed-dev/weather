/**
 * アメダス地点別データの取得（親）。**tgsvr から呼ばれて**子を並列に起こす。
 *
 * なぜ cron をやめたか
 * --------------------
 * 以前は親自身が cron で起き、日時も地点一覧も自分で決めていた。しかし
 * 転置・NetCDF 封入・サイト生成は結局 tgsvr でしかできない。**いつ・何を
 * 取るかを決めるのは tgsvr** に寄せたほうが、日付の切り替わりや取りこぼしの
 * 追跡が 1 箇所で済む。Worker は「重い取得を肩代わりする手足」に徹する。
 *
 * それでも Worker を使うのは tgsvr の負荷を下げるため。1,286 地点を
 * 1 秒間隔で取ると 21 分かかるが、Worker なら並列に走る。
 *
 * なぜ親子に分けるか
 * ------------------
 * 無料枠は **1 実行あたりサブリクエスト 50 件**。1,286 地点を 1 実行では
 * 取れない。子をステートレス（地点を引数で受ける）にすると、**同じ子を
 * 何度でも並列に呼べる**ので、スクリプトは親と子の 2 本で済む。
 *
 *   親 = 子の呼び出し数（≤ 50）
 *   子 = 担当地点数（≤ 50）
 *
 * 環境変数:
 *   CHILD  Service Binding … 子 Worker
 *   TOKEN  呼び出しの合言葉（tgsvr だけが起こせるように）
 */

const KIDS = 33;              // 1 回の呼び出しで起こす子の数（サブリクエスト 50 の内側）

async function run(env, body) {
  const { stations, day, hour } = body;
  const size = Math.ceil(stations.length / KIDS);
  const calls = [];
  for (let i = 0; i < stations.length; i += size) {
    const chunk = stations.slice(i, i + size);
    calls.push(env.CHILD.fetch("https://child/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stations: chunk, day, hour }),
    }).then(r => r.json(), e => ({ error: String(e) })));
  }
  const res = await Promise.all(calls);
  const sum = (k) => res.reduce((n, r) => n + (r[k] || 0), 0);
  return {
    day, hour, stations: stations.length, children: calls.length,
    stored: sum("stored"), missing: sum("missing"), failed: sum("error"),
  };
}

export default {
  /**
   * tgsvr からの呼び出し口。地点一覧は**呼ぶ側が渡す** — Worker が
   * station/index.json を読みに行かないので、地点の増減も tgsvr 側の
   * 一存で反映できる（Worker の再デプロイが要らない）。
   *
   *   POST /fetch  {"day":"20260828","hour":"12",
   *                 "stations":[["11001","011000"], ...]}
   */
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/fetch") {
      return new Response("not found", { status: 404 });
    }
    if (!env.TOKEN || request.headers.get("Authorization") !== `Bearer ${env.TOKEN}`) {
      return new Response("forbidden", { status: 403 });
    }
    let body;
    try {
      body = await request.json();
    } catch (_) {
      return new Response("bad request", { status: 400 });
    }
    const { stations, day, hour } = body || {};
    if (!Array.isArray(stations) || !stations.length
        || !/^\d{8}$/.test(day || "") || !/^\d{2}$/.test(hour || "")) {
      return new Response("stations(配列) と day(YYYYMMDD) と hour(HH) が要る",
                          { status: 400 });
    }
    try {
      return Response.json(await run(env, { stations, day, hour }));
    } catch (e) {
      return new Response(String(e), { status: 500 });
    }
  },
};
