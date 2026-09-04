export type LogFields = Record<string, unknown>;

export function log(level: "info" | "warn" | "error", msg: string, fields: LogFields = {}) {
  const line = JSON.stringify({ level, msg, time: new Date().toISOString(), ...fields });
  if (level === "error") console.error(line);
  else console.log(line);
}
