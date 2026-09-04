import { query } from "./db.js";

export type Account = {
  id: number;
  name: string;
  rateLimitPerMin: number;
};

// one atomic statement. the where clause is the guard, so ten concurrent
// requests cannot each pass a check and then all spend.
export async function reserveCredits(accountId: number, points: number): Promise<number | null> {
  const { rows } = await query<{ remaining: number }>(
    `update accounts
     set credits_used = credits_used + $2
     where id = $1
       and suspended_at is null
       and credits_used + $2 <= credits_granted
     returning credits_granted - credits_used as remaining`,
    [accountId, points],
  );
  return rows[0]?.remaining ?? null;
}

export async function refundCredits(accountId: number, points: number) {
  await query("update accounts set credits_used = greatest(0, credits_used - $2) where id = $1", [
    accountId,
    points,
  ]);
}

export async function getUsage(accountId: number) {
  const { rows } = await query<{
    credits_granted: number;
    credits_used: number;
    rate_limit_per_min: number;
  }>("select credits_granted, credits_used, rate_limit_per_min from accounts where id = $1", [
    accountId,
  ]);
  const row = rows[0];
  if (!row) return null;
  return {
    credits_granted: row.credits_granted,
    credits_used: row.credits_used,
    credits_remaining: row.credits_granted - row.credits_used,
    rate_limit_per_min: row.rate_limit_per_min,
  };
}

export async function findOrCreateAccount(name: string) {
  const { rows } = await query<{ id: number }>(
    `insert into accounts (name) values ($1)
     on conflict (name) do update set name = excluded.name
     returning id`,
    [name],
  );
  return rows[0]!.id;
}
