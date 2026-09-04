import { loadSecrets } from "./secrets.js";

// secrets have to land in process.env before config.ts is evaluated, so the
// app and config are imported dynamically after this resolves
await loadSecrets();

const { app } = await import("./app.js");
const { config } = await import("./config.js");
const { migrate } = await import("./db.js");

await migrate();

app.listen(config.port, () => {
  console.log(JSON.stringify({ level: "info", msg: "listening", port: config.port }));
});
