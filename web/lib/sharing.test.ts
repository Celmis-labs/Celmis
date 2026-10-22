import { describe, expect, it } from "vitest";

import {
  EMPTY_NARROW, MAX_SHARES_PER_TOKEN, blockConfirmKey, canTick, createBody, createProblem,
  daysLeft, emptyCreate, extendBody, extendFormOf, extendProblem, hasPlace, heldBy, narrowBody,
  narrowIntroKey, narrowProblem, refusalKey, revokeReasonKey, sharesToSave, splitGlobs,
  wasShortened, wholeNumber, workspaceOptions,
} from "./sharing";

const NOW = new Date("2026-10-22T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const inDays = (n: number) => new Date(NOW.getTime() + n * DAY).toISOString();

const share = {
  expires_at: inDays(20), allow_ask: true, max_people: 8, holders: 3,
  deny_globs: ["docs/private/**"],
};

describe("splitGlobs", () => {
  it("reads one pattern per line, without blanks or repeats", () => {
    expect(splitGlobs(" a/** \n\n b\r\na/**\n")).toEqual(["a/**", "b"]);
    expect(splitGlobs("")).toEqual([]);
  });
});

describe("wholeNumber", () => {
  it("accepts digits only", () => {
    expect(wholeNumber(" 12 ")).toBe(12);
    expect(wholeNumber("1.5")).toBeNull();
    expect(wholeNumber("-3")).toBeNull();
    expect(wholeNumber("")).toBeNull();
    expect(wholeNumber("1e3")).toBeNull();
  });
});

describe("daysLeft", () => {
  it("rounds up and never goes below zero", () => {
    expect(daysLeft(inDays(2), NOW)).toBe(2);
    expect(daysLeft(new Date(NOW.getTime() + 1000).toISOString(), NOW)).toBe(1);
    expect(daysLeft(inDays(-3), NOW)).toBe(0);
    expect(daysLeft("not a date", NOW)).toBe(0);
  });
});

describe("refusalKey", () => {
  it("says the known refusals in the reader's language", () => {
    expect(refusalKey(404, "No such workspace.")).toBe("sharing.error.workspace");
    expect(refusalKey(409, "This repository is already shared with that workspace. Extend it instead."))
      .toBe("sharing.error.exists");
    expect(refusalKey(409, "Too many active shares.")).toBe("sharing.error.limit");
    expect(refusalKey(409, "widening a share needs a Celmis Enterprise licence (sharing)"))
      .toBe("sharing.error.widen");
  });

  it("leaves the server's own sentence for anything else", () => {
    expect(refusalKey(422, "days is 1 to 90")).toBeNull();
    expect(refusalKey(403, "Admin only.")).toBeNull();
  });

  it("reads a 403 from the licensed routes as the licence lapsing", () => {
    expect(refusalKey(403, "Enterprise feature not licensed.", true)).toBe("sharing.licenceNeeded");
  });
});

describe("narrowing a share", () => {
  it("sends nothing for an empty form", () => {
    expect(narrowBody(EMPTY_NARROW, share, NOW)).toBeNull();
    expect(narrowProblem(EMPTY_NARROW, share, NOW)).toBeNull();
  });

  it("hides only the paths that are not hidden yet", () => {
    const body = narrowBody({ ...EMPTY_NARROW, addDeny: "docs/private/**\nsecrets/**" }, share, NOW);
    expect(body).toEqual({ add_deny: ["secrets/**"] });
    expect(narrowBody({ ...EMPTY_NARROW, addDeny: "docs/private/**" }, share, NOW)).toBeNull();
  });

  it("ends the share sooner and never later", () => {
    const sooner = narrowBody({ ...EMPTY_NARROW, days: "5" }, share, NOW);
    expect(sooner?.expires_at).toBe(inDays(5));
    expect(narrowProblem({ ...EMPTY_NARROW, days: "20" }, share, NOW)).toBe("sharing.narrowDialog.badDays");
    expect(narrowProblem({ ...EMPTY_NARROW, days: "45" }, share, NOW)).toBe("sharing.narrowDialog.badDays");
    expect(narrowProblem({ ...EMPTY_NARROW, days: "0" }, share, NOW)).toBe("sharing.narrowDialog.badDays");
    expect(narrowBody({ ...EMPTY_NARROW, days: "45" }, share, NOW)).toBeNull();
  });

  it("turns questions off only while they are on", () => {
    expect(narrowBody({ ...EMPTY_NARROW, askOff: true }, share, NOW)).toEqual({ allow_ask: false });
    expect(narrowBody({ ...EMPTY_NARROW, askOff: true }, { ...share, allow_ask: false }, NOW)).toBeNull();
  });

  it("lowers the number of people, never below those who hold a token", () => {
    expect(narrowBody({ ...EMPTY_NARROW, people: "4" }, share, NOW)).toEqual({ max_people: 4 });
    expect(narrowProblem({ ...EMPTY_NARROW, people: "2" }, share, NOW)).toBe("sharing.narrowDialog.badPeople");
    expect(narrowProblem({ ...EMPTY_NARROW, people: "8" }, share, NOW)).toBe("sharing.narrowDialog.badPeople");
    expect(narrowProblem({ ...EMPTY_NARROW, people: "9" }, share, NOW)).toBe("sharing.narrowDialog.badPeople");
    expect(narrowProblem({ ...EMPTY_NARROW, people: "3" }, share, NOW)).toBeNull();
  });

  it("never builds a body that widens", () => {
    const body = narrowBody(
      { addDeny: "x/**", days: "5", askOff: true, people: "4" }, share, NOW) ?? {};
    expect(Object.keys(body).sort()).toEqual(["add_deny", "allow_ask", "expires_at", "max_people"]);
    expect(body.allow_ask).toBe(false);
  });
});

describe("creating a share", () => {
  it("starts from the defaults the server uses", () => {
    const form = emptyCreate();
    expect(form.days).toBe("30");
    expect(form.people).toBe("5");
    expect(form.ask).toBe(false);
  });

  it("needs a repository and a workspace", () => {
    expect(createProblem(emptyCreate(), 90)).toBe("sharing.dialog.incomplete");
    expect(createProblem({ ...emptyCreate(), repo: "shop" }, 90)).toBe("sharing.dialog.incomplete");
    expect(createProblem({ ...emptyCreate(), repo: "shop", workspace: "partner" }, 90)).toBeNull();
  });

  it("keeps the days under the server's ceiling and the people within 1 to 25", () => {
    const ok = { ...emptyCreate(), repo: "shop", workspace: "partner" };
    expect(createProblem({ ...ok, days: "91" }, 90)).toBe("sharing.dialog.badDays");
    expect(createProblem({ ...ok, days: "90" }, 90)).toBeNull();
    expect(createProblem({ ...ok, days: "0" }, 90)).toBe("sharing.dialog.badDays");
    expect(createProblem({ ...ok, people: "0" }, 90)).toBe("sharing.dialog.badPeople");
    expect(createProblem({ ...ok, people: "26" }, 90)).toBe("sharing.dialog.badPeople");
    expect(createProblem({ ...ok, people: "25" }, 90)).toBeNull();
  });

  it("sends questions off unless the owner turned them on", () => {
    const body = createBody({ ...emptyCreate(), repo: " shop ", workspace: " partner ", deny: "a\n\nb" });
    expect(body).toEqual({
      repo_slug: "shop", workspace: "partner", days: 30, deny_globs: ["a", "b"],
      max_people: 5, allow_ask: false,
    });
    expect(createBody({ ...emptyCreate(), repo: "r", workspace: "w", ask: true }).allow_ask).toBe(true);
  });
});

describe("the receiving-workspace picker", () => {
  it("offers the id and reads the display name", () => {
    expect(workspaceOptions([
      { id: "w-1", slug: "research", name: "Research" },
      { id: "w-2", slug: "partner", name: "Partner" },
    ])).toEqual([
      { value: "w-1", label: "Research" },
      { value: "w-2", label: "Partner" },
    ]);
  });

  it("adds the slug where two workspaces read the same, case aside", () => {
    expect(workspaceOptions([
      { id: "w-1", slug: "research", name: "Team" },
      { id: "w-2", slug: "partner", name: "team" },
      { id: "w-3", slug: "other", name: "Other" },
    ])).toEqual([
      { value: "w-1", label: "Team (research)" },
      { value: "w-2", label: "team (partner)" },
      { value: "w-3", label: "Other" },
    ]);
  });

  it("falls back to the slug for a workspace without a name", () => {
    expect(workspaceOptions([{ id: "w-1", slug: "research", name: " " }])).toEqual([
      { value: "w-1", label: "research" },
    ]);
  });

  it("offers nothing when there is nothing to offer", () => {
    expect(workspaceOptions([])).toEqual([]);
  });

  it("is a form the create body accepts as it is", () => {
    const [first] = workspaceOptions([{ id: "w-1", slug: "research", name: "Research" }]);
    expect(createProblem({ ...emptyCreate(), repo: "shop", workspace: first.value }, 90)).toBeNull();
  });
});

describe("extending a share", () => {
  it("starts from the share and sends nothing until something differs", () => {
    const form = extendFormOf(share);
    expect(form).toEqual({ days: "", deny: "docs/private/**", ask: true, people: "8" });
    expect(extendBody(form, share)).toBeNull();
    expect(extendProblem(form, share, 90, NOW)).toBeNull();
  });

  it("sends only the fields that changed", () => {
    const form = { ...extendFormOf(share), days: "60", ask: false, people: "10" };
    expect(extendBody(form, share)).toEqual({ days: 60, allow_ask: false, max_people: 10 });
    expect(extendBody({ ...extendFormOf(share), deny: "" }, share)).toEqual({ deny_globs: [] });
  });

  it("needs an end later than the current one and under the ceiling", () => {
    const base = extendFormOf(share);
    expect(extendProblem({ ...base, days: "20" }, share, 90, NOW)).toBe("sharing.extendDialog.notLater");
    expect(extendProblem({ ...base, days: "10" }, share, 90, NOW)).toBe("sharing.extendDialog.notLater");
    expect(extendProblem({ ...base, days: "21" }, share, 90, NOW)).toBeNull();
    expect(extendProblem({ ...base, days: "91" }, share, 90, NOW)).toBe("sharing.dialog.badDays");
  });
});

describe("giving a share to a person", () => {
  const open = { share_id: "a", holders: 2, max_people: 5 };
  const full = { share_id: "b", holders: 5, max_people: 5 };

  it("has a place while fewer people hold it than the owner allowed", () => {
    expect(hasPlace(open, false)).toBe(true);
    expect(hasPlace(full, false)).toBe(false);
  });

  it("keeps the place of a person who already holds it", () => {
    expect(hasPlace(full, true)).toBe(true);
  });

  it("lets a ticked share be unticked even when it is full", () => {
    expect(canTick(full, ["b"], false)).toBe(true);
    expect(canTick(full, [], false)).toBe(false);
    expect(canTick(open, [], false)).toBe(true);
  });

  it("stops at the most a token may carry", () => {
    const ids = Array.from({ length: MAX_SHARES_PER_TOKEN }, (_, i) => `s${i}`);
    expect(canTick(open, ids, false)).toBe(false);
    expect(canTick({ ...open, share_id: "s3" }, ids, false)).toBe(true);
  });

  it("finds the shares a person holds in a workspace on live tokens only", () => {
    const tokens = [
      { user_id: "u1", workspace_id: "w1", status: "active", shares: [{ share_id: "a" }] },
      { user_id: "u1", workspace_id: "w1", status: "revoked", shares: [{ share_id: "b" }] },
      { user_id: "u1", workspace_id: "w2", status: "active", shares: [{ share_id: "c" }] },
      { user_id: "u2", workspace_id: "w1", status: "active", shares: [{ share_id: "d" }] },
      { user_id: "u1", workspace_id: "w1", status: "active" },
    ];
    expect([...heldBy(tokens, "u1", "w1")]).toEqual(["a"]);
  });
});

describe("a token cut short by a share", () => {
  it("is recognised when it ends well before the days asked for", () => {
    expect(wasShortened(inDays(10), 30, true, NOW)).toBe(true);
    expect(wasShortened(inDays(30), 30, true, NOW)).toBe(false);
    expect(wasShortened(inDays(29.5), 30, true, NOW)).toBe(false);
  });

  it("is never claimed for a token without shares", () => {
    expect(wasShortened(inDays(10), 30, false, NOW)).toBe(false);
  });
});

describe("saving the shares of a token", () => {
  it("sends nothing when the list is the same, in any order", () => {
    expect(sharesToSave(["a", "b"], ["b", "a"])).toBeNull();
    expect(sharesToSave([], [])).toBeNull();
  });

  it("sends the whole wanted list when it differs", () => {
    expect(sharesToSave(["a"], ["a", "b"])).toEqual(["a", "b"]);
    expect(sharesToSave(["a", "b"], ["a"])).toEqual(["a"]);
    expect(sharesToSave(["a"], [])).toEqual([]);
  });
});

describe("revokeReasonKey", () => {
  it("words the reasons the server records and nothing else", () => {
    expect(revokeReasonKey("owner")).toBe("sharing.reason.owner");
    expect(revokeReasonKey("repo_removed")).toBe("sharing.reason.repo_removed");
    expect(revokeReasonKey("something new")).toBeNull();
    expect(revokeReasonKey("toString")).toBeNull();
    expect(revokeReasonKey(null)).toBeNull();
  });
});

describe("the words that depend on the licence", () => {
  it("do not tell a licensed build that it lacks the licence", () => {
    expect(narrowIntroKey(true)).toBe("sharing.narrowDialog.introLicensed");
    expect(blockConfirmKey(true)).toBe("sharing.blockConfirmLicensed");
  });
  it("say what giving more would need where the licensed routes are absent", () => {
    expect(narrowIntroKey(false)).toBe("sharing.narrowDialog.intro");
    expect(blockConfirmKey(false)).toBe("sharing.blockConfirm");
  });
});
