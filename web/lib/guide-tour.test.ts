import { describe, expect, it } from "vitest";

import {
  groupByRoute, isInAppRoute, resolveGroup, sanitizeSteps, selectorOf,
  type GuideEnv, type GuideStep,
} from "./guide-tour";

const step = (target: string, route = "/settings/llm", extra: Partial<GuideStep> = {}): GuideStep =>
  ({ target, text: `text ${target}`, route, ...extra });

describe("sanitizeSteps", () => {
  it("keeps well-formed steps and trims text", () => {
    const out = sanitizeSteps([
      { target: "review-agent-max-output:security", text: "  set it ", route: "/review-settings?section=categories&agent=security", opener: "review-agent-toggle:security" },
      { target: "llm-apply-all", text: "x", route: "/settings/llm" },
    ]);
    expect(out).toHaveLength(2);
    expect(out[0].text).toBe("set it");
    expect(out[0].opener).toBe("review-agent-toggle:security");
  });

  it("drops anything that could become an unsafe selector or navigation", () => {
    expect(sanitizeSteps([
      { target: 'a"] , body [x="', text: "x", route: "/a" },
      { target: "ok", text: "x", route: "https://evil.example/" },
      { target: "ok", text: "x", route: "//evil.example" },
      { target: "ok", text: "x", route: "/a\\b" },
      { target: "ok", text: "", route: "/a" },
      { target: "ok", text: "x", route: "/a", opener: "bad opener" },
      null, 7, "str",
    ])).toEqual([]);
  });

  it("is safe on non-arrays and caps the length", () => {
    expect(sanitizeSteps(undefined)).toEqual([]);
    expect(sanitizeSteps({})).toEqual([]);
    const many = Array.from({ length: 20 }, (_, i) => ({ target: `t${i}`, text: "x", route: "/a" }));
    expect(sanitizeSteps(many)).toHaveLength(8);
  });
});

describe("routes and grouping", () => {
  it("accepts only in-app paths", () => {
    expect(isInAppRoute("/settings/llm")).toBe(true);
    expect(isInAppRoute("/a?b=c#d")).toBe(true);
    expect(isInAppRoute("//x")).toBe(false);
    expect(isInAppRoute("javascript:alert(1)")).toBe(false);
    expect(isInAppRoute("/a b")).toBe(false);
  });

  it("builds the data-tour selector", () => {
    expect(selectorOf("review-agent-toggle:defect")).toBe('[data-tour="review-agent-toggle:defect"]');
  });

  it("groups consecutive steps of one page", () => {
    const g = groupByRoute([step("a"), step("b"), step("c", "/x"), step("d")]);
    expect(g.map((x) => x.map((s) => s.target))).toEqual([["a", "b"], ["c"], ["d"]]);
  });
});

/** A page whose elements appear after `appearAt[id]` ms of fake time, and an
 *  opener that reveals its container when clicked. */
function fakeEnv(opts: {
  here?: string;
  appearAt?: Record<string, number>;
  hiddenUntilClick?: Record<string, string>; // target -> opener id
  expanded?: string[];
  cancelAt?: number;
}) {
  let now = 0;
  const clicked: string[] = [];
  const went: string[] = [];
  const revealed = new Set<string>();
  const mk = (id: string) => ({
    id,
    getAttribute: (n: string) =>
      n === "aria-expanded" ? (opts.expanded?.includes(id) ? "true" : "false") : null,
  }) as unknown as HTMLElement;
  const env: GuideEnv = {
    here: () => opts.here ?? "/elsewhere",
    go: (r) => { went.push(r); },
    find: (id) => {
      if (opts.hiddenUntilClick?.[id]) return revealed.has(id) ? mk(id) : null;
      const at = opts.appearAt?.[id];
      return at !== undefined && now >= at ? mk(id) : null;
    },
    click: (el) => {
      const id = (el as unknown as { id: string }).id;
      clicked.push(id);
      for (const [t, o] of Object.entries(opts.hiddenUntilClick ?? {})) if (o === id) revealed.add(t);
    },
    sleep: async (ms) => { now += ms; },
    cancelled: () => opts.cancelAt !== undefined && now >= opts.cancelAt,
  };
  return { env, clicked, went };
}

describe("resolveGroup", () => {
  it("navigates only when the page differs and waits for the control to mount", async () => {
    const { env, went } = fakeEnv({ appearAt: { a: 900 } });
    const r = await resolveGroup([step("a")], env);
    expect(went).toEqual(["/settings/llm"]);
    expect(r[0].el).not.toBeNull();

    const same = fakeEnv({ here: "/settings/llm", appearAt: { a: 0 } });
    await resolveGroup([step("a")], same.env);
    expect(same.went).toEqual([]);
  });

  it("presses the opener once to reveal a folded control", async () => {
    const { env, clicked } = fakeEnv({
      here: "/p", appearAt: { toggle: 0 },
      hiddenUntilClick: { field: "toggle" },
    });
    const r = await resolveGroup(
      [step("field", "/p", { opener: "toggle" })], env);
    expect(clicked).toEqual(["toggle"]);
    expect(r[0].el).not.toBeNull();
  });

  it("does not collapse an opener that is already open", async () => {
    const { env, clicked } = fakeEnv({
      here: "/p", appearAt: { toggle: 0 }, expanded: ["toggle"],
      hiddenUntilClick: { field: "toggle" },
    });
    const r = await resolveGroup([step("field", "/p", { opener: "toggle" })], env);
    expect(clicked).toEqual([]);
    expect(r[0].el).toBeNull();
  });

  it("skips the opener step when its container is already open", async () => {
    const { env, clicked } = fakeEnv({
      here: "/p", appearAt: { toggle: 0, field: 0 }, expanded: ["toggle"],
    });
    const r = await resolveGroup([
      step("toggle", "/p"),
      step("field", "/p", { opener: "toggle" }),
    ], env);
    expect(clicked).toEqual([]);
    expect(r.map((x) => x.step.target)).toEqual(["field"]);
  });

  it("keeps the opener step when its container is closed", async () => {
    const { env } = fakeEnv({ here: "/p", appearAt: { toggle: 0, field: 0 } });
    const r = await resolveGroup([
      step("toggle", "/p"),
      step("field", "/p", { opener: "toggle" }),
    ], env);
    expect(r.map((x) => x.step.target)).toEqual(["toggle", "field"]);
  });

  it("returns el: null for a control that never appears, after a bounded wait", async () => {
    const { env } = fakeEnv({ here: "/p", appearAt: {} });
    const r = await resolveGroup([step("ghost", "/p")], env);
    expect(r).toEqual([{ step: step("ghost", "/p"), el: null }]);
  });

  it("stops waiting when cancelled", async () => {
    const { env } = fakeEnv({ here: "/p", appearAt: {}, cancelAt: 300 });
    const r = await resolveGroup([step("a", "/p"), step("b", "/p")], env);
    expect(r).toHaveLength(1);
  });
});
