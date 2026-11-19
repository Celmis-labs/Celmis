/**
 * Pure helpers of the knowledge-base view (`components/docs/knowledge-base.tsx`),
 * kept out of the component so they are tested without a DOM.
 */

import type { KbFilters, KbGraph } from "@/lib/api";

export const GRAPH_W = 900;
export const GRAPH_H = 560;

/** True when any filter narrows the list (an empty string or false does not). */
export const filtersActive = (f: KbFilters) =>
  Object.values(f).some((v) => v !== undefined && v !== "" && v !== false);

const W = GRAPH_W;
const H = GRAPH_H;

/** Positions for the graph: a few rounds of a plain force layout, started from
 * a circle so the result is the same on every render. Past 220 nodes the
 * circle alone (the force pass is quadratic). */
export function layout(graph: KbGraph): Map<string, { x: number; y: number }> {
  const n = graph.nodes.length;
  const pos = graph.nodes.map((_, i) => ({
    x: W / 2 + (W / 2.6) * Math.cos((2 * Math.PI * i) / Math.max(n, 1)),
    y: H / 2 + (H / 2.6) * Math.sin((2 * Math.PI * i) / Math.max(n, 1)),
  }));
  const index = new Map(graph.nodes.map((node, i) => [node.path, i]));
  if (n > 1 && n <= 220) {
    const k = Math.sqrt((W * H) / n) * 0.6;
    for (let round = 0; round < 120; round++) {
      const temp = 40 * (1 - round / 120) + 1;
      const disp = pos.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const dx = pos[i].x - pos[j].x || 0.01;
          const dy = pos[i].y - pos[j].y || 0.01;
          const d = Math.max(Math.hypot(dx, dy), 0.01);
          const f = (k * k) / d;
          disp[i].x += (dx / d) * f; disp[i].y += (dy / d) * f;
          disp[j].x -= (dx / d) * f; disp[j].y -= (dy / d) * f;
        }
      }
      for (const e of graph.edges) {
        const a = index.get(e.source);
        const b = index.get(e.target);
        if (a === undefined || b === undefined || a === b) continue;
        const dx = pos[a].x - pos[b].x;
        const dy = pos[a].y - pos[b].y;
        const d = Math.max(Math.hypot(dx, dy), 0.01);
        const f = (d * d) / k;
        disp[a].x -= (dx / d) * f; disp[a].y -= (dy / d) * f;
        disp[b].x += (dx / d) * f; disp[b].y += (dy / d) * f;
      }
      for (let i = 0; i < n; i++) {
        const d = Math.max(Math.hypot(disp[i].x, disp[i].y), 0.01);
        pos[i].x = Math.min(W - 30, Math.max(30, pos[i].x + (disp[i].x / d) * Math.min(d, temp)));
        pos[i].y = Math.min(H - 20, Math.max(20, pos[i].y + (disp[i].y / d) * Math.min(d, temp)));
      }
    }
  }
  return new Map(graph.nodes.map((node, i) => [node.path, pos[i]]));
}

