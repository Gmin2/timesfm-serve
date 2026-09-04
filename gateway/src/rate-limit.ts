import type { NextFunction, Request, Response } from "express";

import { config } from "./config.js";
import { query } from "./db.js";
import { log } from "./logger.js";

// fixed window counter kept in postgres. one upsert per request, no redis to
// run or pay for, and the window resets cleanly so there is no state to sweep
// except old rows.
export async function rateLimit(req: Request, res: Response, next: NextFunction) {
  const keyHash = req.keyHash;
  const limit = req.account?.rateLimitPerMin;
  if (!keyHash || !limit) {
    next();
    return;
  }

  const windowMs = config.rateLimitWindowMs;
  const windowStart = new Date(Math.floor(Date.now() / windowMs) * windowMs);

  try {
    const { rows } = await query<{ count: number }>(
      `insert into rate_limit_counters (key_hash, window_start, count)
       values ($1, $2, 1)
       on conflict (key_hash, window_start)
       do update set count = rate_limit_counters.count + 1
       returning count`,
      [keyHash, windowStart],
    );

    const count = rows[0]?.count ?? 1;
    const resetSeconds = Math.ceil((windowStart.getTime() + windowMs - Date.now()) / 1000);
    res.setHeader("x-ratelimit-limit", limit);
    res.setHeader("x-ratelimit-remaining", Math.max(0, limit - count));
    res.setHeader("x-ratelimit-reset", resetSeconds);

    if (count > limit) {
      res.setHeader("retry-after", resetSeconds);
      res.status(429).json({ error: "rate limit exceeded" });
      return;
    }

    // old windows are dead weight, sweep them now and then
    if (Math.random() < 0.01) {
      query("delete from rate_limit_counters where window_start < now() - interval '1 hour'").catch(
        (err: unknown) => log("error", "rate limit sweep failed", { reason: String(err) }),
      );
    }

    next();
  } catch (err) {
    next(err);
  }
}
