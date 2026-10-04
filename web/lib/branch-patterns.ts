/**
 * Target-branch patterns, the web half of src/review/branch_patterns.py.
 *
 * A list of names and fnmatch globs (`release/*`), each optionally negated
 * with a leading `!`. Exclusion wins; a list of exclusions only means "every
 * branch except those"; an empty list means every branch. The settings page
 * uses this to say, under the editor, what a given branch would do — so the
 * sentence on screen and the gate that runs are the same rule. A test runs
 * both on the same cases.
 */

export const NEGATION = "!";

export function cleanPatterns(patterns: readonly string[] | null | undefined): string[] {
  const out: string[] = [];
  for (const raw of patterns ?? []) {
    const entry = String(raw ?? "").trim();
    if (entry && !out.includes(entry)) out.push(entry);
  }
  return out;
}

export function splitPatterns(
  patterns: readonly string[] | null | undefined,
): { includes: string[]; excludes: string[] } {
  const includes: string[] = [];
  const excludes: string[] = [];
  for (const entry of cleanPatterns(patterns)) {
    if (entry.startsWith(NEGATION)) {
      const body = entry.slice(NEGATION.length).trim();
      if (body) excludes.push(body);
    } else {
      includes.push(entry);
    }
  }
  return { includes, excludes };
}

/** Python's `fnmatch.translate`, for the subset fnmatch accepts: `*` any run
 *  (slashes included), `?` one character, `[seq]` / `[!seq]` a class, and a
 *  `[` with no closing `]` a literal bracket. */
export function globToRegExp(glob: string): RegExp {
  let out = "";
  let i = 0;
  const n = glob.length;
  while (i < n) {
    const c = glob[i];
    i += 1;
    if (c === "*") {
      out += ".*";
    } else if (c === "?") {
      out += ".";
    } else if (c === "[") {
      let j = i;
      if (j < n && glob[j] === "!") j += 1;
      if (j < n && glob[j] === "]") j += 1;
      while (j < n && glob[j] !== "]") j += 1;
      if (j >= n) {
        out += "\\[";
      } else {
        let body = glob.slice(i, j).replace(/\\/g, "\\\\");
        i = j + 1;
        if (body.startsWith("!")) body = "^" + body.slice(1);
        else if (body.startsWith("^")) body = "\\" + body;
        out += `[${body}]`;
      }
    } else {
      out += c.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&");
    }
  }
  try {
    return new RegExp(`^(?:${out})$`, "s");
  } catch {
    // A class the browser cannot compile matches nothing, which is what an
    // unusable entry does on the server as well.
    return /(?!)/;
  }
}

export function fnmatchCase(name: string, pattern: string): boolean {
  return globToRegExp(pattern).test(name);
}

export type BranchMatchReason =
  | "unrestricted" | "unknown" | "excluded" | "included" | "not_excluded" | "unmatched";

export type BranchMatch = {
  targeted: boolean;
  reason: BranchMatchReason;
  /** The entry that decided it, as configured ("!main", "release/*"). */
  pattern: string | null;
};

export function matchBranch(
  branch: string | null | undefined,
  patterns: readonly string[] | null | undefined,
): BranchMatch {
  const { includes, excludes } = splitPatterns(patterns);
  if (!includes.length && !excludes.length) {
    return { targeted: true, reason: "unrestricted", pattern: null };
  }
  if (!branch) return { targeted: true, reason: "unknown", pattern: null };
  for (const body of excludes) {
    if (fnmatchCase(branch, body)) {
      return { targeted: false, reason: "excluded", pattern: `${NEGATION}${body}` };
    }
  }
  if (!includes.length) return { targeted: true, reason: "not_excluded", pattern: null };
  for (const entry of includes) {
    if (fnmatchCase(branch, entry)) return { targeted: true, reason: "included", pattern: entry };
  }
  return { targeted: false, reason: "unmatched", pattern: null };
}

/** Why an entry can never match, as an i18n key, or null. The refusals
 *  `pattern_error` makes on the server. */
export function patternErrorKey(entry: string): string | null {
  const value = entry.trim();
  if (!value) return null;
  const body = value.startsWith(NEGATION) ? value.slice(NEGATION.length) : value;
  if (!body.trim()) return "reviewSettings.branches.errorBareNegation";
  if (body.startsWith(NEGATION)) return "reviewSettings.branches.errorDoubleNegation";
  if (/\s/.test(body.trim())) return "reviewSettings.branches.errorSpace";
  return null;
}

/** "staging, !master, !main" → the entries; commas and whitespace split. */
export function parsePatternInput(text: string): string[] {
  return cleanPatterns(text.split(/[\s,]+/));
}
