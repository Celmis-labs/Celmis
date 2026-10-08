/**
 * Client-side search over a pull-request list, for the PR picker.
 *
 * Matching is case-insensitive and Unicode-aware, and ignores diacritics
 * ("café" finds "cafe", and the other way round). The query is split on
 * whitespace; every word has to match at least one field (number, title,
 * source branch, target branch, author). A leading "#" on a numeric word is
 * dropped, so "#42" and "42" mean the same.
 *
 * Ranking (lower is better, summed over the words): an exact PR number first,
 * then number prefix, title word-prefix, title substring, branch, author,
 * number substring. Equal scores keep the incoming order (newest first).
 */

export type SearchablePr = {
  number: number;
  title: string;
  source_branch: string | null;
  target_branch: string | null;
  author?: string | null;
};

/** Lower-case and strip combining marks, so accents never block a match. */
export function normalizeText(s: string): string {
  return s.normalize("NFD").replace(/\p{M}+/gu, "").toLowerCase();
}

function wordPrefix(hay: string, needle: string): boolean {
  let from = 0;
  for (;;) {
    const i = hay.indexOf(needle, from);
    if (i < 0) return false;
    if (i === 0 || !/[\p{L}\p{N}]/u.test(hay[i - 1])) return true;
    from = i + 1;
  }
}

const NO_MATCH = Number.POSITIVE_INFINITY;

function scoreWord(pr: SearchablePr, word: string): number {
  const num = String(pr.number);
  const digits = /^\d+$/.test(word);
  if (digits && num === word) return 0;
  // Shorter numbers first: "12" ranks #120 ahead of #1200.
  if (digits && num.startsWith(word)) return 1 + (num.length - word.length) / 100;
  const title = normalizeText(pr.title);
  if (wordPrefix(title, word)) return 2;
  if (title.includes(word)) return 3;
  if (normalizeText(pr.source_branch ?? "").includes(word)) return 4;
  if (normalizeText(pr.target_branch ?? "").includes(word)) return 5;
  if (normalizeText(pr.author ?? "").includes(word)) return 6;
  if (digits && num.includes(word)) return 7;
  return NO_MATCH;
}

export function queryWords(query: string): string[] {
  return normalizeText(query)
    .split(/\s+/)
    .map((w) => (/^#\d+$/.test(w) ? w.slice(1) : w))
    .filter(Boolean);
}

/** Matches for `query`, best first. An empty query returns `prs` unchanged. */
export function filterPrs<T extends SearchablePr>(prs: readonly T[], query: string): T[] {
  const words = queryWords(query);
  if (words.length === 0) return [...prs];
  const scored: Array<{ pr: T; score: number; i: number }> = [];
  prs.forEach((pr, i) => {
    let score = 0;
    for (const w of words) {
      const s = scoreWord(pr, w);
      if (s === NO_MATCH) return;
      score += s;
    }
    scored.push({ pr, score, i });
  });
  scored.sort((a, b) => a.score - b.score || a.i - b.i);
  return scored.map((x) => x.pr);
}
