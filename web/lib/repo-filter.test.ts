import { describe, expect, it } from "vitest";

import { filterRepos } from "@/lib/repo-filter";

const repos = [
  { slug: "acme-web-ui", full_name: "acme/web-ui", provider: "github", display_name: null },
  { slug: "acme-billing", full_name: "acme/billing", provider: "bitbucket", display_name: null },
  { slug: "upload-legacy", full_name: "upload-legacy", provider: "upload", display_name: "Старий облік" },
  { slug: "tools-web-api", full_name: "tools/web-api", provider: "gitlab" },
];

const slugs = (q: string) => filterRepos(repos, q).map((r) => r.slug);

describe("filterRepos", () => {
  it("keeps the whole list, in order, for an empty query", () => {
    expect(slugs("")).toEqual(repos.map((r) => r.slug));
    expect(slugs("   ")).toEqual(repos.map((r) => r.slug));
  });

  it("finds by part of the name, the owner, or both in any order", () => {
    expect(slugs("bill")).toEqual(["acme-billing"]);
    expect(slugs("acme")).toEqual(["acme-web-ui", "acme-billing"]);
    expect(slugs("web acme")).toEqual(["acme-web-ui"]);
    expect(slugs("WEB")).toEqual(["acme-web-ui", "tools-web-api"]);
  });

  it("finds an uploaded archive by the name it was given", () => {
    expect(slugs("облік")).toEqual(["upload-legacy"]);
  });

  it("finds by provider and by slug", () => {
    expect(slugs("bitbucket")).toEqual(["acme-billing"]);
    expect(slugs("tools-web")).toEqual(["tools-web-api"]);
  });

  it("returns nothing when one word matches nothing", () => {
    expect(slugs("acme nowhere")).toEqual([]);
  });

  it("hands back the same objects, not copies", () => {
    expect(filterRepos(repos, "billing")[0]).toBe(repos[1]);
  });
});
