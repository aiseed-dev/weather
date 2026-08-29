/**
 * アメダス地点別データの取得（親）。**tgsvr から呼ばれて**子を並列に起こす。
 *
 * なぜ cron を持たないか
 * ----------------------
 * 転置・NetCDF 封入・サイト生成は結局 tgsvr でしかできない。**いつ・何を
 * 取るかを決めるのは tgsvr** に寄せたほうが、日付の切り替わりや取りこぼしの
 * 追跡が 1 箇所で済む。Worker は「重い取得を肩代わりする手足」に徹する。
 *
 * それでも Worker を使うのは tgsvr の負荷を下げるため。1,286 地点を
 * 1 秒間隔で取ると 21 分かかるが、Worker なら並列に走る。
 *
 * 数の勘定（無料枠はサブリクエスト 50/実行。R2 の put も数に入る）
 * ----------------------------------------------------------------
 *   子 … 担当エリアの地点数ぶんの fetch ＋ R2 put 1 回
 *        最大エリアは 47 地点なので 48。収まる
 *   親 … 子の呼び出し数（Service Binding も 1 件と数える）
 *        1 回の呼び出しで扱えるエリアは MAX_AREAS まで
 *
 * エリアは 64 種あるので、tgsvr は 2 回に分けて呼ぶ。分割を tgsvr 側に
 * 置くのは、取りこぼしたエリアだけ呼び直せるようにするため。
 *
 * 環境変数:
 *   CHILD  Service Binding … 子 Worker
 *   TOKEN  呼び出しの合言葉（tgsvr だけが起こせるように）
 */

const MAX_AREAS = 45;         // 1 実行で起こす子の数（サブリクエスト 50 の内側）
const MAX_STATIONS = 49;      // 子 1 実行の地点数（fetch N + put 1 ≤ 50）

async function run(env, { areas, day, hour }) {
  const calls = areas.map(({ area, stations }) =>
    env.CHILD.fetch("https://child/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ area, stations, day, hour }),
    }).then(r => r.json(), e => ({ area, error: String(e) })));

  const res = await Promise.all(calls);
  const sum = (k) => res.reduce((n, r) => n + (r[k] || 0), 0);
  // 置けなかったエリアは呼ぶ側が拾えるように名前で返す（次回そこだけ呼び直せる）
  const failed = res.filter(r => !r.written).map(r => r.area);
  return {
    day, hour, areas: areas.length,
    stored: sum("stored"), missing: sum("missing"), error: sum("error"),
    written: sum("written"), failed,
  };
}

export default {
  /**
   * tgsvr からの呼び出し口。エリアと地点は**呼ぶ側が渡す** — Worker が
   * 地点表を読みに行かないので、地点の増減も tgsvr の一存で反映できる
   * （Worker の再デプロイが要らない）。
   *
   *   POST /fetch
   *   {"day":"20260829","hour":"12",
   *    "areas":[{"area":"011000","stations":["11001","11016"]}, ...]}
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
    const { areas, day, hour } = body || {};
    if (!Array.isArray(areas) || !areas.length
        || !/^\d{8}$/.test(day || "") || !/^\d{2}$/.test(hour || "")) {
      return new Response("areas(配列) と day(YYYYMMDD) と hour(HH) が要る",
                          { status: 400 });
    }
    if (areas.length > MAX_AREAS) {
      return new Response(`エリアが多すぎます（${areas.length}）。`
                          + `1 回 ${MAX_AREAS} まで。分けて呼んでください`,
                          { status: 400 });
    }
    for (const a of areas) {
      if (!a || !a.area || !Array.isArray(a.stations) || !a.stations.length) {
        return new Response("areas の要素は {area, stations[]} が要る", { status: 400 });
      }
      if (a.stations.length > MAX_STATIONS) {
        return new Response(`${a.area} の地点が多すぎます（${a.stations.length}）。`
                            + `1 エリア ${MAX_STATIONS} まで`, { status: 400 });
      }
    }
    try {
      return Response.json(await run(env, { areas, day, hour }));
    } catch (e) {
      return new Response(String(e), { status: 500 });
    }
  },
};
