import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import pg from "pg";

import { config } from "./config.js";

// postgres bigints arrive as strings by default so nothing is silently lost.
// account ids and credit balances are far below Number.MAX_SAFE_INTEGER, so
// parsing them as numbers here is safe and saves casting at every call site.
pg.types.setTypeParser(20, Number);

// rds certificates chain to an amazon root that node does not ship, so verify
// against the bundled rds ca rather than turning verification off. no ca file
// means local postgres, where tls is not in play at all.
const ca = config.dbCaPath && existsSync(config.dbCaPath) ? readFileSync(config.dbCaPath) : undefined;

export const pool = new pg.Pool({
  connectionString: config.databaseUrl,
  max: config.dbPoolMax,
  ...(ca ? { ssl: { ca, rejectUnauthorized: true } } : {}),
});

export function query<T extends pg.QueryResultRow>(text: string, params?: unknown[]) {
  return pool.query<T>(text, params);
}

const migrationsDir = join(dirname(fileURLToPath(import.meta.url)), "..", "migrations");

// arbitrary but fixed, so every instance takes the same lock
const MIGRATION_LOCK = 8_274_119;

export async function migrate() {
  // several instances can boot at once, on lambda that is the normal case.
  // the lock makes the loser wait and then find nothing left to apply. even
  // "create table if not exists" has to be inside it, because concurrent ddl
  // on the same name races in postgres and throws a duplicate key error.
  //
  // the whole run is one transaction taking a transaction scoped lock, rather
  // than a session lock, because a pgbouncer style pooler in transaction mode
  // (neon's pooled endpoint, for one) does not keep session state between
  // statements and a session lock there would quietly not lock at all.
  const client = await pool.connect();
  try {
    await client.query("begin");
    await client.query("select pg_advisory_xact_lock($1)", [MIGRATION_LOCK]);

    await client.query(
      `create table if not exists schema_migrations (
         name text primary key,
         applied_at timestamptz not null default now()
       )`,
    );

    const { rows } = await client.query<{ name: string }>("select name from schema_migrations");
    const applied = new Set(rows.map((r) => r.name));

    const files = readdirSync(migrationsDir)
      .filter((f) => f.endsWith(".sql"))
      .sort();

    for (const name of files) {
      if (applied.has(name)) continue;
      const sql = readFileSync(join(migrationsDir, name), "utf8");
      await client.query(sql);
      await client.query("insert into schema_migrations (name) values ($1)", [name]);
      console.log(JSON.stringify({ level: "info", msg: "migration applied", name }));
    }

    // postgres ddl is transactional, so the lock and every migration commit
    // together or not at all
    await client.query("commit");
  } catch (err) {
    await client.query("rollback").catch(() => {});
    throw err;
  } finally {
    client.release();
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
