/**
 * Repository sharing: the rules the pages apply before they ask the server.
 *
 * Pure functions, no React and no network, so that `lib/sharing.test.ts` runs
 * them on node and `tests/web` compares the constants with the server's. The
 * server decides every one of these again; what is here only decides what to
 * offer, so that Save is not enabled for a request the API will answer with a
 * 409 or a 422.
 *
 * Two audiences read this file. The owner's page (`/admin/sharing`, with the
 * licensed dialog in `web/ee/sharing`) builds the bodies of narrowing, creating
 * and extending a share. The superadmin's token page (`/admin/mcp-tokens`)
 * decides which shared repositories a person can still be given.
 */

import type {
  McpShare, ShareCreateIn, ShareDetail, ShareExtendIn, ShareNarrowIn, ShareSummary, ShareTarget,
} from "@/lib/api";

// ─── the server's limits (src/access/shares.py) ──────────────────────

/** Most shared repositories one token lists (`MAX_SHARES_PER_TOKEN`). */
export const MAX_SHARES_PER_TOKEN = 10;
/** Most people who may hold a token for one share (`MIN_PEOPLE`, `MAX_PEOPLE`). */
export const MIN_PEOPLE = 1;
export const MAX_PEOPLE = 25;
/** What a new share starts with (`DEFAULT_PEOPLE`, `DEFAULT_DAYS`). */
export const DEFAULT_PEOPLE = 5;
export const DEFAULT_DAYS = 30;
/** Most path patterns one share hides (`MAX_DENY_GLOBS`). */
export const MAX_DENY_GLOBS = 100;

const DAY_MS = 24 * 60 * 60 * 1000;

// ─── small readers ───────────────────────────────────────────────────

/** `YYYY-MM-DD` of an ISO time, as every date on these pages is written. */
export function dayOf(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "";
}

/** `YYYY-MM-DD HH:MM` of an ISO time. */
export function minuteOf(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 16).replace("T", " ") : "";
}

/** Whole days from `now` until `iso`, rounded up; 0 for a time that has passed
 *  or cannot be read. */
export function daysLeft(iso: string, now: Date = new Date()): number {
  const end = Date.parse(iso);
  if (Number.isNaN(end)) return 0;
  return Math.max(0, Math.ceil((end - now.getTime()) / DAY_MS));
}

/** One glob per line, as the dialogs ask for them: trimmed, without blank lines
 *  and without repeats, in the order written. */
export function splitGlobs(text: string): string[] {
  const seen = new Set<string>();
  for (const line of text.split(/\r?\n/)) {
    const glob = line.trim();
    if (glob) seen.add(glob);
  }
  return [...seen];
}

/** A whole number from a text field, or `null` when it is not one. */
export function wholeNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,6}$/.test(trimmed)) return null;
  return Number(trimmed);
}

// ─── why a share ended ───────────────────────────────────────────────

/** `revoke_reason` of the server (`REASONS` in src/access/shares.py) in the words
 *  of the page. Written out key by key so that the keys can be found by search. */
const REVOKE_REASONS = new Map<string, string>([
  ["owner", "sharing.reason.owner"],
  ["superadmin", "sharing.reason.superadmin"],
  ["repo_removed", "sharing.reason.repo_removed"],
  ["workspace_deleted", "sharing.reason.workspace_deleted"],
  ["expired", "sharing.reason.expired"],
]);

/** The i18n key for a reason a share ended, or `null` for one this client does not
 *  know: a raw code is not shown to a reader. */
export function revokeReasonKey(reason: string | null | undefined): string | null {
  return (reason && REVOKE_REASONS.get(reason)) || null;
}

// ─── errors ──────────────────────────────────────────────────────────

/** The sentences of the server (`src/ee/sharing/service.py`, `src/access/shares.py`)
 *  that the pages say in the reader's language. Anything else is shown as the
 *  server wrote it. */
const KNOWN_REFUSALS: Array<[RegExp, string]> = [
  [/^No such workspace\.?$/i, "sharing.error.workspace"],
  [/already shared with that workspace/i, "sharing.error.exists"],
  [/^Too many active shares\.?$/i, "sharing.error.limit"],
  [/widening a share needs a Celmis Enterprise licence/i, "sharing.error.widen"],
];

/** The i18n key that says a refusal in the reader's language, or `null` when the
 *  server's own sentence is the better one. A 403 from the licensed routes is the
 *  licence lapsing while the page is open. */
export function refusalKey(status: number, message: string, licensed = false): string | null {
  if (licensed && status === 403) return "sharing.licenceNeeded";
  for (const [pattern, key] of KNOWN_REFUSALS) {
    if (pattern.test(message)) return key;
  }
  return null;
}

// ─── words that depend on the licence ────────────────────────────────

/** The sentence that opens the narrow dialog. Where the licensed routes are mounted
 *  it points at Extend; where they are not, it says what giving more would need. A
 *  build with the licence is not told it lacks one. */
export function narrowIntroKey(canGrant: boolean): string {
  return canGrant ? "sharing.narrowDialog.introLicensed" : "sharing.narrowDialog.intro";
}

/** The question asked before a person is switched off: it says how to switch them
 *  back on, and that this needs the licence only where it does. */
export function blockConfirmKey(canGrant: boolean): string {
  return canGrant ? "sharing.blockConfirmLicensed" : "sharing.blockConfirm";
}

// ─── the owner narrows a share (free) ────────────────────────────────

export type NarrowForm = {
  /** One glob per line: paths to hide on top of the ones already hidden. */
  addDeny: string;
  /** Days from now the share should end. Empty keeps the end. */
  days: string;
  /** Turn questions off. Only offered while they are on. */
  askOff: boolean;
  /** The new, lower, limit of people. Empty keeps it. */
  people: string;
};

export const EMPTY_NARROW: NarrowForm = { addDeny: "", days: "", askOff: false, people: "" };

/** Why a narrowing form cannot be sent, as an i18n key, or `null` when it can.
 *  A share can be made smaller here and never larger: the same rule the API
 *  applies (`shares.narrow`). */
export function narrowProblem(
  form: NarrowForm, share: Pick<ShareSummary, "expires_at" | "max_people" | "holders">,
  now: Date = new Date(),
): string | null {
  if (form.days.trim()) {
    const days = wholeNumber(form.days);
    if (days === null || days < 1 || now.getTime() + days * DAY_MS >= Date.parse(share.expires_at)) {
      return "sharing.narrowDialog.badDays";
    }
  }
  if (form.people.trim()) {
    const people = wholeNumber(form.people);
    if (people === null || people < Math.max(MIN_PEOPLE, share.holders) || people >= share.max_people) {
      return "sharing.narrowDialog.badPeople";
    }
  }
  if (splitGlobs(form.addDeny).length > MAX_DENY_GLOBS) return "sharing.dialog.tooManyPaths";
  return null;
}

/** The body of the PATCH, with only what narrows; `null` when nothing would change.
 *  A pattern that is already hidden is left out, so the audit trail does not
 *  record a change that was not one. */
export function narrowBody(
  form: NarrowForm, share: Pick<ShareDetail, "expires_at" | "allow_ask" | "max_people" | "deny_globs">,
  now: Date = new Date(),
): ShareNarrowIn | null {
  const body: ShareNarrowIn = {};
  const hidden = new Set(share.deny_globs);
  const add = splitGlobs(form.addDeny).filter((g) => !hidden.has(g));
  if (add.length) body.add_deny = add;
  const days = wholeNumber(form.days);
  if (days !== null && days >= 1) {
    const end = new Date(now.getTime() + days * DAY_MS);
    if (end.getTime() < Date.parse(share.expires_at)) body.expires_at = end.toISOString();
  }
  if (form.askOff && share.allow_ask) body.allow_ask = false;
  const people = wholeNumber(form.people);
  if (people !== null && people < share.max_people) body.max_people = people;
  return Object.keys(body).length ? body : null;
}

// ─── the owner makes and extends a share (licensed) ──────────────────

export type CreateForm = {
  repo: string;
  workspace: string;
  days: string;
  deny: string;
  people: string;
  ask: boolean;
};

export function emptyCreate(): CreateForm {
  return { repo: "", workspace: "", days: String(DEFAULT_DAYS), deny: "", people: String(DEFAULT_PEOPLE), ask: false };
}

/** Why a creating form cannot be sent (an i18n key), or `null`. `maxDays` is the
 *  server's ceiling (`GET /api/mcp/tokens/me` → `max_days`). */
export function createProblem(form: CreateForm, maxDays: number): string | null {
  if (!form.repo.trim() || !form.workspace.trim()) return "sharing.dialog.incomplete";
  const days = wholeNumber(form.days);
  if (days === null || days < 1 || days > maxDays) return "sharing.dialog.badDays";
  const people = wholeNumber(form.people);
  if (people === null || people < MIN_PEOPLE || people > MAX_PEOPLE) return "sharing.dialog.badPeople";
  if (splitGlobs(form.deny).length > MAX_DENY_GLOBS) return "sharing.dialog.tooManyPaths";
  return null;
}

/** The choices of the receiving-workspace picker: the id is what is sent, the display name
 *  what is read. Two workspaces may share a name (case aside), so then the slug is added to
 *  tell them apart; a workspace without a name is read by its slug. */
export function workspaceOptions(
  targets: ReadonlyArray<ShareTarget>,
): Array<{ value: string; label: string }> {
  const seen = new Map<string, number>();
  for (const w of targets) {
    const key = w.name.trim().toLowerCase();
    seen.set(key, (seen.get(key) ?? 0) + 1);
  }
  return targets.map((w) => {
    const name = w.name.trim();
    if (name === "") return { value: w.id, label: w.slug };
    const alone = seen.get(name.toLowerCase()) === 1;
    return { value: w.id, label: alone ? name : `${name} (${w.slug})` };
  });
}

export function createBody(form: CreateForm): ShareCreateIn {
  return {
    repo_slug: form.repo.trim(),
    workspace: form.workspace.trim(),
    days: Number(form.days),
    deny_globs: splitGlobs(form.deny),
    max_people: Number(form.people),
    allow_ask: form.ask,
  };
}

export type ExtendForm = {
  /** Days from now. Empty keeps the end. */
  days: string;
  /** The whole list of hidden paths, one per line. */
  deny: string;
  ask: boolean;
  people: string;
};

export function extendFormOf(share: Pick<ShareDetail, "deny_globs" | "allow_ask" | "max_people">): ExtendForm {
  return { days: "", deny: share.deny_globs.join("\n"), ask: share.allow_ask, people: String(share.max_people) };
}

/** Why an extending form cannot be sent (an i18n key), or `null`. */
export function extendProblem(
  form: ExtendForm, share: Pick<ShareDetail, "expires_at">, maxDays: number, now: Date = new Date(),
): string | null {
  if (form.days.trim()) {
    const days = wholeNumber(form.days);
    if (days === null || days < 1 || days > maxDays) return "sharing.dialog.badDays";
    if (now.getTime() + days * DAY_MS <= Date.parse(share.expires_at)) return "sharing.extendDialog.notLater";
  }
  const people = wholeNumber(form.people);
  if (people === null || people < MIN_PEOPLE || people > MAX_PEOPLE) return "sharing.dialog.badPeople";
  if (splitGlobs(form.deny).length > MAX_DENY_GLOBS) return "sharing.dialog.tooManyPaths";
  return null;
}

/** The body of the extend call: only the fields that differ from the share, so a
 *  change of one thing is audited as one thing. `null` when nothing differs. */
export function extendBody(
  form: ExtendForm, share: Pick<ShareDetail, "deny_globs" | "allow_ask" | "max_people">,
): ShareExtendIn | null {
  const body: ShareExtendIn = {};
  const days = wholeNumber(form.days);
  if (days !== null && days >= 1) body.days = days;
  const deny = splitGlobs(form.deny);
  if (deny.join("\n") !== share.deny_globs.join("\n")) body.deny_globs = deny;
  if (form.ask !== share.allow_ask) body.allow_ask = form.ask;
  const people = wholeNumber(form.people);
  if (people !== null && people !== share.max_people) body.max_people = people;
  return Object.keys(body).length ? body : null;
}

// ─── the superadmin gives a share to a person (token page) ───────────

/** Is there still a place in `share` for this person? A person who already holds a
 *  token for it keeps their place, so the share being full does not lock them out
 *  of keeping it on a second token. */
export function hasPlace(share: Pick<McpShare, "holders" | "max_people">, alreadyHolder: boolean): boolean {
  return alreadyHolder || share.holders < share.max_people;
}

/** May this share be ticked? Not when it is full for this person, and not once the
 *  token already lists the most it may carry (a ticked one can always be unticked). */
export function canTick(
  share: Pick<McpShare, "share_id" | "holders" | "max_people">,
  picked: readonly string[], alreadyHolder: boolean,
): boolean {
  if (picked.includes(share.share_id)) return true;
  if (picked.length >= MAX_SHARES_PER_TOKEN) return false;
  return hasPlace(share, alreadyHolder);
}

/** The ids of the shares `userId` already holds a live token for in `workspaceId`. */
export function heldBy(
  tokens: ReadonlyArray<{
    user_id: string; workspace_id: string; status: string; shares?: ReadonlyArray<{ share_id: string }>;
  }>,
  userId: string, workspaceId: string,
): Set<string> {
  const held = new Set<string>();
  for (const token of tokens) {
    if (token.user_id !== userId || token.workspace_id !== workspaceId || token.status !== "active") continue;
    for (const share of token.shares ?? []) held.add(share.share_id);
  }
  return held;
}

/** Was the token cut short by a share? The server ends a token with the earliest
 *  share on it, so a token that ends noticeably before the days asked for was. A
 *  day of slack covers the rounding of "30 days" against a clock. */
export function wasShortened(
  expiresAt: string, askedDays: number, hasShares: boolean, now: Date = new Date(),
): boolean {
  if (!hasShares) return false;
  const end = Date.parse(expiresAt);
  if (Number.isNaN(end)) return false;
  return end < now.getTime() + (askedDays - 1) * DAY_MS;
}

/** The ids to send when a token's list of shares is saved, or `null` when the list
 *  did not change (nothing to PATCH). The order of the server's list is kept for
 *  the ones that stay, so a save with no change is not a change. */
export function sharesToSave(current: readonly string[], wanted: readonly string[]): string[] | null {
  const same = current.length === wanted.length && current.every((id) => wanted.includes(id));
  return same ? null : [...wanted];
}
