import { describe, expect, it } from "vitest";

import {
  AUTO_RUN_WINDOW_MS, planUiAction, sanitizeUiAction, shouldAutoRun,
} from "./ui-action";

const steps = [
  { target: "setting:base_instruction", text: "Write the rules every agent follows.", route: "/review-settings?repo=acme-shop&section=prompts" },
  { target: "prompt-guidelines:defect", text: "Extra guidelines for this agent.", route: "/review-settings?repo=acme-shop&section=prompts" },
];
const raw = {
  ok: true, mode: "tour", title: "Review settings > Custom prompts",
  route: "/review-settings?repo=acme-shop&section=prompts", repo: "acme-shop",
  workspace_id: "ws-1", steps,
};

describe("sanitizeUiAction", () => {
  it("keeps a well-formed action with its steps", () => {
    const ui = sanitizeUiAction(raw, undefined);
    expect(ui?.mode).toBe("tour");
    expect(ui?.steps).toHaveLength(2);
    expect(ui?.repo).toBe("acme-shop");
  });

  it("refuses a route that leaves the app", () => {
    expect(sanitizeUiAction({ ...raw, route: "https://evil.example/" }, undefined)).toBeNull();
    expect(sanitizeUiAction({ ...raw, route: "//evil.example" }, undefined)).toBeNull();
    expect(sanitizeUiAction(null, undefined)).toBeNull();
    expect(sanitizeUiAction("x", undefined)).toBeNull();
  });

  it("drops malformed steps instead of throwing", () => {
    const ui = sanitizeUiAction({ ...raw, steps: [{ target: 'a"]', text: "x", route: "/a" }, 3] }, undefined);
    expect(ui?.steps).toEqual([]);
  });

  it("treats an unknown mode as a tour and reads the roles of a link", () => {
    expect(sanitizeUiAction({ ...raw, mode: "weird" }, undefined)?.mode).toBe("tour");
    const link = sanitizeUiAction({ ...raw, mode: "link", needs: ["admin", 5, "owner"] }, undefined);
    expect(link?.mode).toBe("link");
    expect(link?.needs).toEqual(["admin", "owner"]);
  });
});

describe("planUiAction", () => {
  const ui = sanitizeUiAction(raw, undefined)!;

  it("runs the tour on the same workspace", () => {
    expect(planUiAction(ui, "ws-1")).toEqual({ kind: "tour", steps: ui.steps });
    expect(planUiAction(ui, "")).toEqual({ kind: "tour", steps: ui.steps });
  });

  it("does nothing for an answer given in another workspace than the active one", () => {
    expect(planUiAction(ui, "ws-2")).toEqual({ kind: "stale" });
  });

  it("only opens the page when the role cannot change it (permission fallback)", () => {
    const link = { ...ui, mode: "link" as const };
    expect(planUiAction(link, "ws-1")).toEqual({ kind: "open", route: ui.route });
  });

  it("only opens the page when no step survived validation", () => {
    expect(planUiAction({ ...ui, steps: [] }, "ws-1")).toEqual({ kind: "open", route: ui.route });
  });
});

describe("shouldAutoRun", () => {
  const now = 1_000_000;
  it("starts a fresh answer once", () => {
    expect(shouldAutoRun("r1", now - 5_000, now, new Set())).toBe(true);
    expect(shouldAutoRun("r1", now - 5_000, now, new Set(["r1"]))).toBe(false);
  });
  it("does not replay an old answer reloaded from history", () => {
    expect(shouldAutoRun("r1", now - AUTO_RUN_WINDOW_MS - 1, now, new Set())).toBe(false);
  });
  it("ignores a row without an id or a usable time", () => {
    expect(shouldAutoRun("", now, now, new Set())).toBe(false);
    expect(shouldAutoRun("r1", Number.NaN, now, new Set())).toBe(false);
  });
});
