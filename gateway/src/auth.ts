import { createHash, randomBytes } from "node:crypto";

import type { NextFunction, Request, Response } from "express";

import { query } from "./db.js";

const PREFIX = "tfm_";

// api keys are 24 random bytes, so a plain fast hash is the right tool here.
// bcrypt and argon2 exist to slow down guessing of low entropy human passwords
// and would add tens of milliseconds to every single request for no gain.
export function hashKey(key: string) {
  return createHash("sha256").update(key).digest("hex");
}

export async function createApiKey(accountId: number, label?: string) {
  const key = PREFIX + randomBytes(24).toString("base64url");
  await query("insert into api_keys (hash, prefix, account_id, label) values ($1, $2, $3, $4)", [
    hashKey(key),
    key.slice(0, PREFIX.length + 6),
    accountId,
    label ?? null,
  ]);
  return key;
}

export async function requireKey(req: Request, res: Response, next: NextFunction) {
  const key = req.header("x-api-key");
  if (!key) {
    res.status(401).json({ error: "missing api key" });
    return;
  }
  const keyHash = hashKey(key);
  try {
    const { rows } = await query<{ id: number; name: string; rate_limit_per_min: number }>(
      `select a.id, a.name, a.rate_limit_per_min
       from api_keys k
       join accounts a on a.id = k.account_id
       where k.hash = $1 and k.revoked_at is null and a.suspended_at is null`,
      [keyHash],
    );
    const row = rows[0];
    if (!row) {
      res.status(401).json({ error: "invalid api key" });
      return;
    }
    req.account = { id: row.id, name: row.name, rateLimitPerMin: row.rate_limit_per_min };
    req.keyHash = keyHash;
    next();
  } catch (err) {
    next(err);
  }
}
