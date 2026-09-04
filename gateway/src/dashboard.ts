import { type NextFunction, type Request, type Response, Router } from "express";
import { fromNodeHeaders } from "better-auth/node";

import { getUsage } from "./accounts.js";
import { createApiKey } from "./auth.js";
import { auth, githubConfigured } from "./better-auth.js";
import { config } from "./config.js";
import { query } from "./db.js";
import { log } from "./logger.js";

/**
 * every signed in github user gets exactly one account, created on first
 * visit with the signup credit grant. the account is keyed by the better auth
 * user id so key rotation and re-login never touch the balance.
 */
async function accountForUser(user: { id: string; name?: string | null; email?: string | null }) {
  const existing = await query<{ id: number }>("select id from accounts where user_id = $1", [
    user.id,
  ]);
  if (existing.rows[0]) return existing.rows[0].id;

  const name = user.email ?? user.name ?? `user-${user.id}`;
  const { rows } = await query<{ id: number }>(
    `insert into accounts (name, email, user_id, credits_granted)
     values ($1, $2, $3, $4)
     on conflict (user_id) do update set user_id = excluded.user_id
     returning id`,
    [name, user.email ?? null, user.id, config.signupCredits],
  );
  log("info", "account created", { userId: user.id, accountId: rows[0]!.id });
  return rows[0]!.id;
}

async function requireSession(req: Request, res: Response, next: NextFunction) {
  try {
    const session = await auth.api.getSession({ headers: fromNodeHeaders(req.headers) });
    if (!session?.user) {
      res.status(401).json({ error: "not signed in" });
      return;
    }
    req.accountId = await accountForUser(session.user);
    req.userEmail = session.user.email ?? null;
    next();
  } catch (err) {
    next(err);
  }
}

export const dashboard = Router();

dashboard.get("/dash/config", (_req, res) => {
  res.json({ github_configured: githubConfigured, signup_credits: config.signupCredits });
});

dashboard.get("/dash/me", requireSession, async (req: Request, res: Response, next) => {
  try {
    const usage = await getUsage(req.accountId!);
    res.json({ email: req.userEmail, ...usage });
  } catch (err) {
    next(err);
  }
});

dashboard.get("/dash/keys", requireSession, async (req: Request, res: Response, next) => {
  try {
    const { rows } = await query(
      `select id, prefix, label, created_at, revoked_at
       from api_keys where account_id = $1 order by id desc`,
      [req.accountId],
    );
    res.json({ keys: rows });
  } catch (err) {
    next(err);
  }
});

dashboard.post("/dash/keys", requireSession, async (req: Request, res: Response, next) => {
  try {
    const label = typeof req.body?.label === "string" ? req.body.label.slice(0, 60) : undefined;
    const key = await createApiKey(req.accountId!, label);
    // the only time the caller ever sees the key, it is a hash from here on
    res.status(201).json({ key, note: "copy this now, it cannot be shown again" });
  } catch (err) {
    next(err);
  }
});

dashboard.delete("/dash/keys/:id", requireSession, async (req: Request, res: Response, next) => {
  try {
    const { rowCount } = await query(
      "update api_keys set revoked_at = now() where id = $1 and account_id = $2 and revoked_at is null",
      [Number(req.params.id), req.accountId],
    );
    if (!rowCount) {
      res.status(404).json({ error: "key not found" });
      return;
    }
    res.status(204).end();
  } catch (err) {
    next(err);
  }
});
