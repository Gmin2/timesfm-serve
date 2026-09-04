import { betterAuth } from "better-auth";

import { config } from "./config.js";
import { pool } from "./db.js";

/**
 * better auth owns the user, session, account and verification tables. our
 * accounts table links to it by user id rather than adding billing columns to
 * a schema their migrations manage.
 */
export const githubConfigured = Boolean(config.githubClientId && config.githubClientSecret);

export const auth = betterAuth({
  database: pool,
  baseURL: config.baseUrl,
  secret: config.authSecret,
  emailAndPassword: { enabled: false },
  // registering a provider with empty credentials turns every sign in attempt
  // into a 500, so leave it out until it is actually configured
  socialProviders: githubConfigured
    ? {
        github: {
          clientId: config.githubClientId,
          clientSecret: config.githubClientSecret,
        },
      }
    : {},
});

export type Session = typeof auth.$Infer.Session;
