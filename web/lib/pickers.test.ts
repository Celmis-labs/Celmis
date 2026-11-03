import { describe, expect, it } from "vitest";

import {
  comboStep, filterOptions, selectAllState, toggleAllVisible, type PickerOption,
} from "@/lib/pickers";

const people: PickerOption[] = [
  { value: "1", label: "alice@example.com", hint: "Alice Smith" },
  { value: "2", label: "bob@example.com", hint: "Bob Jones" },
  { value: "3", label: "carol@example.org", hint: "Carol Smith" },
];

describe("filterOptions", () => {
  it("keeps everything for an empty query", () => {
    expect(filterOptions(people, "  ")).toHaveLength(3);
  });
  it("matches email or name, every word, ignoring case and accents", () => {
    expect(filterOptions(people, "SMITH").map((o) => o.value)).toEqual(["1", "3"]);
    expect(filterOptions(people, "smith carol").map((o) => o.value)).toEqual(["3"]);
    expect(filterOptions([{ value: "x", label: "Zoë" }], "zoe")).toHaveLength(1);
  });
  it("searches hidden keywords such as a slug", () => {
    const ws = [{ value: "w", label: "Platform", keywords: ["acme-platform"] }];
    expect(filterOptions(ws, "acme-pl")).toHaveLength(1);
    expect(filterOptions(ws, "nothing")).toHaveLength(0);
  });
});

describe("comboStep", () => {
  it("opens on ArrowDown, then moves and wraps", () => {
    let s = comboStep({ open: false, active: 0 }, "ArrowDown", 3);
    expect(s).toMatchObject({ open: true, active: 0, handled: true });
    s = comboStep(s, "ArrowDown", 3);
    expect(s.active).toBe(1);
    s = comboStep({ open: true, active: 2 }, "ArrowDown", 3);
    expect(s.active).toBe(0);
    s = comboStep({ open: true, active: 0 }, "ArrowUp", 3);
    expect(s.active).toBe(2);
  });
  it("Enter picks the highlighted row and closes", () => {
    expect(comboStep({ open: true, active: 1 }, "Enter", 3))
      .toMatchObject({ pick: true, open: false, active: 1, handled: true });
  });
  it("Enter does nothing on a closed or empty list (the form can submit)", () => {
    expect(comboStep({ open: false, active: 0 }, "Enter", 3).handled).toBe(false);
    expect(comboStep({ open: true, active: 0 }, "Enter", 0).pick).toBe(false);
  });
  it("Escape closes only an open list", () => {
    expect(comboStep({ open: true, active: 0 }, "Escape", 3)).toMatchObject({ open: false, handled: true });
    expect(comboStep({ open: false, active: 0 }, "Escape", 3).handled).toBe(false);
  });
  it("Home and End jump", () => {
    expect(comboStep({ open: true, active: 1 }, "Home", 5).active).toBe(0);
    expect(comboStep({ open: true, active: 1 }, "End", 5).active).toBe(4);
  });
});

describe("select all", () => {
  const ids = ["a", "b", "c", "d"];
  it("reads none / some / all", () => {
    expect(selectAllState(ids, []).state).toBe("none");
    expect(selectAllState(ids, ["a"])).toMatchObject({ state: "some", ticked: 1, total: 4 });
    expect(selectAllState(ids, ids).state).toBe("all");
  });
  it("ticks everything, then clears everything", () => {
    const all = toggleAllVisible(ids, ["b"]);
    expect([...all].sort()).toEqual(ids);
    expect(toggleAllVisible(ids, all)).toEqual([]);
  });
  it("acts on the visible rows only and leaves hidden ones alone", () => {
    const next = toggleAllVisible(["a", "b"], ["d"]);
    expect([...next].sort()).toEqual(["a", "b", "d"]);
    expect(toggleAllVisible(["a", "b"], next)).toEqual(["d"]);
  });
  it("respects a cap and an untickable row", () => {
    const canAdd = (id: string, picked: readonly string[]) => id !== "b" && picked.length < 2;
    const next = toggleAllVisible(ids, [], canAdd);
    expect(next).toEqual(["a", "c"]);
    // b can't be ticked and the cap is reached: what is left reads as "all", so one click clears.
    expect(selectAllState(ids, next, canAdd).state).toBe("all");
    expect(toggleAllVisible(ids, next, canAdd)).toEqual([]);
  });
  it("an empty list is none and ticks nothing", () => {
    expect(selectAllState([], []).state).toBe("none");
    expect(toggleAllVisible([], ["x"])).toEqual(["x"]);
  });
});
