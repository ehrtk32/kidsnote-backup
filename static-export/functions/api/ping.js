// B단계 확인용. Functions 가 실제로 배포·실행되는지, D1 바인딩이 붙는지만 본다.
export async function onRequestGet({ env }) {
  const out = {
    ok: true,
    at: new Date().toISOString(),
    hasDB: Boolean(env.DB),
  };
  if (env.DB) {
    try {
      const { results } = await env.DB
        .prepare(
          "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_cf%' ESCAPE '\\' ORDER BY name"
        )
        .all();
      out.tables = results.map((row) => row.name);
    } catch (err) {
      out.dbError = String(err);
    }
  }
  return Response.json(out);
}
