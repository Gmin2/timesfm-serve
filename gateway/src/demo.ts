import { readFileSync } from "node:fs";

import { Router } from "express";

import { config } from "./config.js";

// state capital, close enough for a state level weather covariate
const COORDS: Record<string, [number, number]> = {
  Karnataka: [12.97, 77.59],
  Gujarat: [23.03, 72.58],
  Delhi: [28.61, 77.21],
  Maharashtra: [19.08, 72.88],
  "Tamil Nadu": [13.08, 80.27],
};

type Point = { date: string; demand: number };

let history: Map<string, Point[]> | undefined;

function loadHistory() {
  if (history) return history;
  const csv = readFileSync(config.demandCsvPath, "utf8").trim().split("\n");
  const header = csv[0]!.split(",");
  const iDate = header.indexOf("date");
  const iState = header.indexOf("state");
  const iDemand = header.indexOf("demand");

  const out = new Map<string, Point[]>();
  for (const line of csv.slice(1)) {
    const cols = line.split(",");
    const state = cols[iState]!;
    let rows = out.get(state);
    if (!rows) out.set(state, (rows = []));
    rows.push({ date: cols[iDate]!, demand: Number(cols[iDemand]) });
  }
  history = out;
  return out;
}

export const demo = Router();

demo.get("/demo/states", (_req, res) => {
  res.json(
    Object.entries(COORDS).map(([state, [lat, lon]]) => ({ state, lat, lon })),
  );
});

demo.get("/demo/history", (req, res) => {
  const state = String(req.query.state ?? "");
  const days = Math.min(Number(req.query.days ?? 365) || 365, 2000);
  const rows = loadHistory().get(state);
  if (!rows) {
    res.status(404).json({ error: "unknown state" });
    return;
  }
  const coords = COORDS[state]!;
  const slice = rows.slice(-days);
  res.json({
    state,
    lat: coords[0],
    lon: coords[1],
    dates: slice.map((r) => r.date),
    demand: slice.map((r) => r.demand),
  });
});
