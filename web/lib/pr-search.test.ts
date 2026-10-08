import { describe, expect, it } from "vitest";

import { filterPrs, type SearchablePr } from "./pr-search";

const pr = (
  number: number,
  title: string,
  source_branch: string | null = "feature/ABC-12",
  target_branch: string | null = "main",
  author = "alex",
): SearchablePr => ({ number, title, source_branch, target_branch, author });

const list = [
  pr(1200, "Fix order totals"),
  pr(12, "Add retry to uploads", "feature/ABC-12"),
  pr(120, "Update docs", "docs/readme", "release/1.2"),
  pr(7, "Виправити підрахунок суми", "fix/sum", "main", "Олена"),
  pr(8, "Café menu rendering", "ui/menu", "develop", "renée"),
];
const nums = (q: string) => filterPrs(list, q).map((p) => p.number);

describe("filterPrs", () => {
  it("returns everything in order for an empty query", () => {
    expect(nums("")).toEqual([1200, 12, 120, 7, 8]);
    expect(nums("   ")).toEqual([1200, 12, 120, 7, 8]);
  });

  it("ranks the exact PR number first, with or without #", () => {
    expect(nums("12")[0]).toBe(12);
    expect(nums("#12")[0]).toBe(12);
    expect(nums("#12").slice(0, 3)).toEqual([12, 120, 1200]);
  });

  it("matches the title, case-insensitively", () => {
    expect(nums("ORDER")).toEqual([1200]);
  });

  it("matches Cyrillic titles and authors", () => {
    expect(nums("підрахунок")).toEqual([7]);
    expect(nums("СУМИ")).toEqual([7]);
    expect(nums("олена")).toEqual([7]);
  });

  it("ignores diacritics both ways", () => {
    expect(nums("cafe")).toEqual([8]);
    expect(nums("café")).toEqual([8]);
    expect(nums("renee")).toEqual([8]);
  });

  it("matches source and target branches", () => {
    expect(nums("readme")).toEqual([120]);
    expect(nums("release")).toEqual([120]);
    expect(nums("ui/menu")).toEqual([8]);
  });

  it("requires every word to match", () => {
    expect(nums("fix totals")).toEqual([1200]);
    expect(nums("fix nothing")).toEqual([]);
  });

  it("puts title word-prefix ahead of a mid-word hit", () => {
    const l = [pr(1, "Refix thing"), pr(2, "Fix thing")];
    expect(filterPrs(l, "fix").map((p) => p.number)).toEqual([2, 1]);
  });

  it("tolerates null branches", () => {
    expect(filterPrs([pr(3, "x", null, null)], "main")).toEqual([]);
  });
});
