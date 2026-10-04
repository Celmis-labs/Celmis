/** Ignore-glob editing helpers, shared by the repo policy page and the
 *  workspace review defaults page — both edit the same list with the same
 *  server-side rules (`validate_ignore_globs` in src/review/ignore_globs.py). */

/** One pattern per line → the list the API stores, blank lines dropped. */
export function globLines(text: string): string[] {
  return text.split("\n").map((l) => l.trim()).filter(Boolean);
}

/** Whether a `[...]` class in the pattern would not compile — the server's
 *  `_tokens` builds `[body]` (a leading `!` negates) and refuses on a
 *  regex error, e.g. a reversed range `[z-a]`. */
export function globBadClass(line: string): boolean {
  let i = 0;
  while ((i = line.indexOf("[", i)) !== -1) {
    const end = line.indexOf("]", i + 2);
    if (end === -1) return false;
    let body = line.slice(i + 1, end).replace(/\\/g, "\\\\");
    if (body.startsWith("!")) body = "^" + body.slice(1);
    try {
      new RegExp(`[${body}]`);
    } catch {
      return true;
    }
    i = end + 1;
  }
  return false;
}

/** The first problem with a glob list, as an i18n key + the offending line,
 *  or null. The same refusals `validate_ignore_globs` makes on the server, so
 *  a bad pattern is caught at the keyboard rather than as a 422 on Save. */
export function globError(lines: string[]): { key: string; line: string } | null {
  for (const line of lines) {
    if (line.startsWith("!")) return { key: "review.settings.globNegation", line };
    if (line.startsWith("#")) return { key: "review.settings.globComment", line };
    if (/[\t\r\n]/.test(line)) return { key: "review.settings.globWhitespace", line };
    if (line.replace(/[/*]/g, "") === "") return { key: "review.settings.globEverything", line };
    if (line.length > 300) return { key: "review.settings.globTooLong", line: line.slice(0, 40) };
    // MAX_STARS in src/review/ignore_globs.py.
    if ((line.match(/\*/g) ?? []).length > 8) return { key: "review.settings.globTooManyStars", line };
    if (globBadClass(line)) return { key: "review.settings.globBadClass", line };
  }
  if (lines.length > 200) return { key: "review.settings.globTooMany", line: String(lines.length) };
  return null;
}
