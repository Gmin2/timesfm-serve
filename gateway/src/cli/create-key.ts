import { createApiKey } from "../auth.js";
import { migrate, pool } from "../db.js";

const tenant = process.argv[2] ?? "dev";
await migrate();
console.log(await createApiKey(tenant));
await pool.end();
