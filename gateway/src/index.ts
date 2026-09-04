import { app } from "./app.js";
import { config } from "./config.js";
import { migrate } from "./db.js";

await migrate();

app.listen(config.port, () => {
  console.log(JSON.stringify({ level: "info", msg: "listening", port: config.port }));
});
