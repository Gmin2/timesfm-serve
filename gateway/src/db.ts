import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import pg from "pg";

import { config } from "./config.js";

// postgres bigints arrive as strings by default so nothing is silently lost.
// account ids and credit balances are far below Number.MAX_SAFE_INTEGER, so
// parsing them as numbers here is safe and saves casting at every call site.
pg.types.setTypeParser(20, Number);

export const pool = new pg.Pool({
  connectionString: config.databaseUrl,
  max: config.dbPoolMax,
});

export function query<T extends pg.QueryResultRow>(text: string, params?: unknown[]) {
  return pool.query<T>(text, params);
}

const migrationsDir = join(dirname(fileURLToPath(import.meta.url)), "..", "migrations");

export async function migrate() {
  await query(
    `create table if not exists schema_migrations (
       name text primary key,
       applied_at timestamptz not null default now()
     )`,
  );
  const { rows } = await query<{ name: string }>("select name from schema_migrations");
  const applied = new Set(rows.map((r) => r.name));

  const files = readdirSync(migrationsDir)
    .filter((f) => f.endsWith(".sql"))
    .sort();

  for (const name of files) {
    if (applied.has(name)) continue;
    const sql = readFileSync(join(migrationsDir, name), "utf8");
    const client = await pool.connect();
    try {
      await client.query("begin");
      await client.query(sql);
      await client.query("insert into schema_migrations (name) values ($1)", [name]);
      await client.query("commit");
      console.log(JSON.stringify({ level: "info", msg: "migration applied", name }));
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }
  }
}

// append only ledger. every credit spent has a row here, so a balance that
// looks wrong can always be recomputed and traced back to the calls behind it.
export async function recordRun(run: {
  accountId: number;
  model: string;
  horizon: number;
  contextLen: number;
  nPastCov: number;
  nFutureCov: number;
  points: number;
  latencyMs: number;
  requestId?: string;
}) {
  await query(
    `insert into forecast_runs
       (account_id, model, horizon, context_len, n_past_cov, n_future_cov, points, latency_ms, request_id)
     values ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
    [
      run.accountId,
      run.model,
      run.horizon,
      run.contextLen,
      run.nPastCov,
      run.nFutureCov,
      run.points,
      run.latencyMs,
      run.requestId ?? null,
    ],
  );
}
