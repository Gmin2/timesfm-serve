import { GetParametersCommand, SSMClient } from "@aws-sdk/client-ssm";

const NAMES = [
  "DATABASE_URL",
  "BETTER_AUTH_SECRET",
  "GITHUB_CLIENT_ID",
  "GITHUB_CLIENT_SECRET",
  "APP_BASE_URL",
] as const;

/**
 * pulls secrets from ssm parameter store into process.env at cold start, so
 * nothing sensitive sits in the lambda config where the console shows it.
 * a no-op when SSM_PREFIX is unset, which is how it stays out of the way
 * locally and in tests.
 *
 * must run before config.ts is imported, so index.ts imports the app
 * dynamically after awaiting this.
 */
export async function loadSecrets() {
  const prefix = process.env.SSM_PREFIX;
  if (!prefix) return;

  const client = new SSMClient({});
  const res = await client.send(
    new GetParametersCommand({
      Names: NAMES.map((n) => `${prefix}/${n}`),
      WithDecryption: true,
    }),
  );

  for (const p of res.Parameters ?? []) {
    const key = p.Name?.split("/").pop();
    if (key && p.Value) process.env[key] = p.Value;
  }

  const missing = res.InvalidParameters ?? [];
  if (missing.length) {
    console.log(
      JSON.stringify({ level: "warn", msg: "ssm parameters not found", missing }),
    );
  }
}
