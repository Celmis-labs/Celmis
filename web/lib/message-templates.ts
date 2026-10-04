/**
 * Review message templates, the web half of `message_template_error` and
 * `render_message_template` in src/review/review_defaults.py.
 *
 * A template is plain text with `{placeholder}` fields; `{{` and `}}` write a
 * literal brace. Only the placeholders the server lists are accepted — a typo
 * would otherwise be posted literally on every pull request — and no format
 * spec or conversion (`{commit:>8}`, `{files!r}`). The same parse as Python's
 * `string.Formatter`, so what this accepts is what the server accepts and the
 * preview is what a pull request will show.
 */

export type TemplateToken =
  | { kind: "text"; text: string }
  | { kind: "field"; name: string; raw: string };

export type TemplateError =
  | { key: "reviewSettings.messages.errorUnknown"; field: string }
  | { key: "reviewSettings.messages.errorSpec"; field: string }
  | { key: "reviewSettings.messages.errorUnclosed" }
  | { key: "reviewSettings.messages.errorStrayBrace" };

export function parseTemplate(
  text: string,
): { tokens: TemplateToken[]; error: TemplateError | null } {
  const tokens: TemplateToken[] = [];
  let buf = "";
  let i = 0;
  const flush = () => {
    if (buf) tokens.push({ kind: "text", text: buf });
    buf = "";
  };
  while (i < text.length) {
    const c = text[i];
    if (c === "{") {
      if (text[i + 1] === "{") {
        buf += "{";
        i += 2;
        continue;
      }
      const end = text.indexOf("}", i + 1);
      if (end === -1) return { tokens, error: { key: "reviewSettings.messages.errorUnclosed" } };
      const raw = text.slice(i + 1, end);
      flush();
      tokens.push({ kind: "field", name: raw, raw: `{${raw}}` });
      i = end + 1;
      continue;
    }
    if (c === "}") {
      if (text[i + 1] === "}") {
        buf += "}";
        i += 2;
        continue;
      }
      return { tokens, error: { key: "reviewSettings.messages.errorStrayBrace" } };
    }
    buf += c;
    i += 1;
  }
  flush();
  return { tokens, error: null };
}

/** The first problem with `text` as a template, or null. */
export function templateError(text: string, placeholders: readonly string[]): TemplateError | null {
  const { tokens, error } = parseTemplate(text);
  if (error) return error;
  for (const token of tokens) {
    if (token.kind !== "field") continue;
    if (/[:!]/.test(token.name)) {
      return { key: "reviewSettings.messages.errorSpec", field: token.raw };
    }
    if (!placeholders.includes(token.name)) {
      return { key: "reviewSettings.messages.errorUnknown", field: token.raw };
    }
  }
  return null;
}

/** `text` with its placeholders filled from `values`; a field without a
 *  value renders empty, as on the server. */
export function renderTemplate(text: string, values: Record<string, string>): string {
  const { tokens, error } = parseTemplate(text);
  if (error) return text;
  return tokens
    .map((tok) => (tok.kind === "text" ? tok.text : values[tok.name] ?? ""))
    .join("");
}

/** Sample values for the live preview — one plausible pull request. */
export const SAMPLE_VALUES: Record<string, string> = {
  commit: "4f2c9a1",
  agents: "defect, contract, security",
  files: "12",
  pr_number: "418",
};
