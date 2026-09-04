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

export async function createApiKey(tenant: string) {
  const key = PREFIX + randomBytes(24).toString("base64url");
  await query("insert into api_keys (hash, prefix, tenant) values ($1, $2, $3)", [
    hashKey(key),
    key.slice(0, PREFIX.length + 6),
    tenant,
  ]);
  return key;
}

export async function requireKey(req: Request, res: Response, next: NextFunction) {
  const key = req.header("x-api-key");
  if (!key) {
    res.status(401).json({ error: "missing api key" });
    return;
  }
  try {
    const { rows } = await query<{ tenant: string }>(
      "select tenant from api_keys where hash = $1 and revoked_at is null",
      [hashKey(key)],
    );
    const row = rows[0];
    if (!row) {
      res.status(401).json({ error: "invalid api key" });
      return;
    }
    req.tenant = row.tenant;
    next();
  } catch (err) {
    next(err);
  }
}
