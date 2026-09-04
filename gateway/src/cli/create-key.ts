import { findOrCreateAccount } from "../accounts.js";
import { createApiKey } from "../auth.js";
import { migrate, pool } from "../db.js";

const name = process.argv[2] ?? "dev";
const label = process.argv[3];

await migrate();
const accountId = await findOrCreateAccount(name);
console.log(await createApiKey(accountId, label));
await pool.end();
