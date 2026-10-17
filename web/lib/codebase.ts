/**
 * Pure helpers for the Codebase section (knowledge packs).
 *
 * No imports, on purpose: `tests/web` compiles this file on its own and runs
 * it under node, so the rules a page leans on — how a refused upload is read,
 * how items are grouped, what a folder pick leaves out — are checked as code
 * and not as a description of code.
 */

/** Who a pack is shown to, widest first. `PACK_AUDIENCES` in src/users/roles.py. */
export const PACK_AUDIENCES = ["workspace", "teams", "restricted"] as const;
/** What a team or a person may do with a pack. `PACK_PERMISSIONS` in src/users/roles.py. */
export const PACK_PERMISSIONS = ["read", "maintain"] as const;

/** What the server accepts in one upload (`src/knowledge/ingest.py`). The
 *  page states these and refuses an oversize pick before sending it; the
 *  server enforces them either way. */
export const UPLOAD_LIMITS = {
  /** The request body: a zip, or the files of a folder together. */
  archiveBytes: 5 * 1024 * 1024,
  files: 400,
  fileBytes: 512 * 1024,
  totalBytes: 8 * 1024 * 1024,
} as const;

/** The server's refusal of an upload: a machine code, a sentence, and for a
 *  secret scan the places it found something — never the matched text. */
export type PackIngestHit = { path: string; line: number; label: string };
export type PackIngestError = { code: string; message: string; hits: PackIngestHit[] };

/** The structured refusal inside an error from the upload call, or null for
 *  any other failure (network, 403, a plain `detail` string). Reads
 *  `ApiError.body` by shape so this file needs no import. */
export function refusalOf(error: unknown): PackIngestError | null {
  const body = (error as { body?: unknown } | null | undefined)?.body;
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const d = detail as { code?: unknown; message?: unknown; hits?: unknown };
  if (typeof d.code !== "string" || typeof d.message !== "string") return null;
  const hits: PackIngestHit[] = [];
  if (Array.isArray(d.hits)) {
    for (const h of d.hits) {
      if (!h || typeof h !== "object") continue;
      const { path, line, label } = h as Record<string, unknown>;
      if (typeof path !== "string") continue;
      hits.push({
        path,
        line: typeof line === "number" ? line : 0,
        label: typeof label === "string" ? label : "",
      });
    }
  }
  return { code: d.code, message: d.message, hits };
}

/** `path:line label`, the way a secret-scan hit is shown. A hit with no line
 *  (a whole file refused) is just `path label`. */
export function hitLabel(hit: PackIngestHit): string {
  const where = hit.line > 0 ? `${hit.path}:${hit.line}` : hit.path;
  return hit.label ? `${where} ${hit.label}` : where;
}

/** Extensions of the text formats a pack keeps (`TEXT_EXTENSIONS` in
 *  src/knowledge/ingest.py; a test holds the two lists equal). The server
 *  ignores every other file, so such a file is not one of the pack's files. */
export const TEXT_EXTENSIONS = [
  ".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
  ".sh", ".bash", ".zsh", ".ps1", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
  ".java", ".kt", ".kts", ".rb", ".php", ".cs", ".sql", ".http", ".rest", ".graphql", ".gql",
  ".proto", ".xml", ".html", ".css", ".scss", ".tf", ".hcl", ".env.example", ".properties",
  ".gradle", ".csv",
] as const;

/** The server ignores a path with a segment that starts with a dot and the
 *  macOS resource-fork folder (`_is_hidden` in src/knowledge/ingest.py). */
export function isHiddenPath(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith(".") || seg === "__MACOSX");
}

/** Whether the file has one of the pack's text extensions (`_extension_allowed`
 *  in src/knowledge/ingest.py): the last extension of the name, or the whole
 *  `.env.example`. A dotfile such as `.md` has no extension. */
export function hasTextExtension(path: string): boolean {
  const low = path.toLowerCase();
  if (low.endsWith(".env.example")) return true;
  const name = low.slice(low.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot >= name.length - 1) return false;
  return (TEXT_EXTENSIONS as readonly string[]).includes(name.slice(dot));
}

/** Whether the server would keep this file of a folder pick. The limit of
 *  `UPLOAD_LIMITS.files` counts these only: an image or a binary next to the
 *  text is ignored by the server and is not one of the 400. */
export function keptByServer(path: string): boolean {
  return !isHiddenPath(path) && hasTextExtension(path);
}

/** A path the folder picker should not send: hidden folders (`.git`), the
 *  macOS resource-fork folder and dependency folders. The server ignores the
 *  first two anyway; this keeps them out of the request body, where a
 *  checked-out repository would otherwise use the whole 5 MB on its own
 *  history. Everything else goes up, and the server says what it ignored. */
export function skippedLocally(path: string): boolean {
  return isHiddenPath(path) || path.split("/").includes("node_modules");
}

/** Human size: 812 B, 3.4 KB, 1.2 MB. */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** A pack slug suggested from its title: lowercase ASCII words joined by
 *  dashes. A title with no Latin letters gives an empty suggestion and the
 *  person types one. */
export function slugify(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

export function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

/** A markdown file's leading `---` block, apart from the text under it. */
export function splitFrontMatter(text: string): { front: string | null; body: string } {
  const m = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(text);
  if (!m) return { front: null, body: text };
  return { front: m[1], body: text.slice(m[0].length) };
}

type ItemLike = { path: string; kind: string; skill: string | null; name: string; description: string };

export type SkillGroup<T> = { skill: string; description: string; items: T[] };
export type KindGroup<T> = { kind: string; items: T[] };

/** The order non-skill kinds are listed in; anything else follows. */
export const KIND_ORDER = ["api", "checklist", "note", "doc"] as const;

/** A pack's files the way a person looks for them: one group per skill (its
 *  SKILL.md first, then the files that belong to it), then the rest by kind. */
export function groupItems<T extends ItemLike>(items: T[]): {
  skills: SkillGroup<T>[];
  others: KindGroup<T>[];
} {
  const bySkill = new Map<string, T[]>();
  const byKind = new Map<string, T[]>();
  for (const item of items) {
    if (item.skill) {
      const list = bySkill.get(item.skill) ?? [];
      list.push(item);
      bySkill.set(item.skill, list);
    } else {
      // "file" without a skill should not happen; it reads as a document.
      const kind = item.kind === "file" || item.kind === "skill" ? "doc" : item.kind;
      const list = byKind.get(kind) ?? [];
      list.push(item);
      byKind.set(kind, list);
    }
  }
  const byPath = (a: ItemLike, b: ItemLike) => a.path.localeCompare(b.path);
  const skills = [...bySkill.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([skill, list]) => {
      const head = list.find((i) => i.kind === "skill");
      const sorted = [...list].sort((a, b) =>
        (a.kind === "skill" ? 0 : 1) - (b.kind === "skill" ? 0 : 1) || byPath(a, b));
      return { skill, description: head?.description ?? "", items: sorted };
    });
  const known: readonly string[] = KIND_ORDER;
  const kinds = [
    ...KIND_ORDER.filter((k) => byKind.has(k)),
    ...[...byKind.keys()].filter((k) => !known.includes(k)).sort(),
  ];
  const others = kinds.map((kind) => ({ kind, items: [...byKind.get(kind)!].sort(byPath) }));
  return { skills, others };
}

/** A path from a version diff. The server lists changed files by path; an
 *  entry that carries more than a bare string still shows its path. */
export function diffPath(entry: unknown): string {
  if (typeof entry === "string") return entry;
  const path = (entry as { path?: unknown } | null | undefined)?.path;
  return typeof path === "string" ? path : "";
}

/** Whether an invite can still be used, and if not why: the Sharing tab shows
 *  a status per link instead of leaving the maintainer to compare dates. */
export type InviteState = "active" | "revoked" | "expired" | "used";
export function inviteState(
  invite: { revoked: boolean; expires_at: string; max_uses: number; used_count: number },
  now: number,
): InviteState {
  if (invite.revoked) return "revoked";
  if (Date.parse(invite.expires_at) <= now) return "expired";
  if (invite.used_count >= invite.max_uses) return "used";
  return "active";
}

/** A restricted pack with no team grant is private: only the people named on it
 *  (its creator is one) and the superadmin see it. The server decides who sees
 *  what; this only names the case so the page can say it in plain words. The
 *  team list is the maintainer's to see, so a reader passes `undefined` and
 *  gets the plain "restricted" wording. */
export function isPrivatePack(audience: string, teamCount: number | undefined): boolean {
  return audience === "restricted" && teamCount === 0;
}

/** The i18n key that names an audience: `codebase.audience.private` for the
 *  private case, `codebase.audience.<audience>` otherwise. */
export function audienceKey(audience: string, teamCount?: number): string {
  return isPrivatePack(audience, teamCount) ? "codebase.audience.private" : `codebase.audience.${audience}`;
}

/** What the server accepts for a publish token (`src/knowledge/store.py`). */
export const PUBLISH_TOKEN_LIMITS = { defaultDays: 90, maxDays: 365, maxActive: 10, labelChars: 80 } as const;

/** Whether a publish token can still publish, and if not why. */
export type TokenState = "active" | "revoked" | "expired";
export function tokenState(token: { revoked: boolean; expires_at: string | null }, now: number): TokenState {
  if (token.revoked) return "revoked";
  if (token.expires_at && Date.parse(token.expires_at) <= now) return "expired";
  return "active";
}

/** The shell lines that publish a pack from a laptop: the address, the token
 *  (by reference, never its value), and the push. `address` is the API base the
 *  CLI talks to; `tokenHint` is the words standing in for the token. */
export function publishCommands(
  address: string, slug: string, tokenHint: string,
): string {
  return [
    `export CELMIS_URL=${address || "<server address>"}`,
    `export CELMIS_PACK_TOKEN=<${tokenHint}>`,
    `celmis-pack push ./skills/${slug || "<folder>"}`,
  ].join("\n");
}
