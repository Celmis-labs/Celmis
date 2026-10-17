"use client";

/**
 * Why the server refused an upload, in the server's own terms.
 *
 * An upload is refused with HTTP 422 and `{code, message, hits}`: a secret
 * scan lists `path:line label` for each place it found something and never
 * the matched text, and this shows exactly that and nothing more. A failure
 * that is not a refusal (network, permission) falls through to its message.
 */

import { useT } from "@/lib/i18n";
import { hitLabel, refusalOf } from "@/lib/codebase";
import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";

/** What to do about a refusal, by its code. A code without an entry shows the
 *  server's sentence alone. */
const HINT_KEYS: Record<string, string> = {
  secrets_found: "codebase.ingest.hint.secrets_found",
  secret_file: "codebase.ingest.hint.secret_file",
  binary_file: "codebase.ingest.hint.binary_file",
  duplicate_skill: "codebase.ingest.hint.duplicate_skill",
  nested_skill: "codebase.ingest.hint.nested_skill",
  bad_front_matter: "codebase.ingest.hint.bad_front_matter",
  no_files: "codebase.ingest.hint.no_files",
  too_many_files: "codebase.ingest.hint.too_many_files",
  file_too_large: "codebase.ingest.hint.file_too_large",
  total_too_large: "codebase.ingest.hint.total_too_large",
  archive_too_large: "codebase.ingest.hint.archive_too_large",
  encrypted: "codebase.ingest.hint.encrypted",
  symlink: "codebase.ingest.hint.symlink",
  unsafe_path: "codebase.ingest.hint.unsafe_path",
  bad_archive: "codebase.ingest.hint.bad_archive",
  duplicate_path: "codebase.ingest.hint.duplicate_path",
  path_too_long: "codebase.ingest.hint.path_too_long",
  too_deep: "codebase.ingest.hint.too_deep",
};

/** More than this many hits are summarised; the first ones are the ones to fix. */
const SHOWN_HITS = 50;

export function IngestError({ error }: { error: unknown }) {
  const t = useT();
  const refusal = refusalOf(error);
  if (!refusal) {
    return <Callout tone="danger">{(error as Error)?.message ?? String(error)}</Callout>;
  }
  const hintKey = HINT_KEYS[refusal.code];
  const shown = refusal.hits.slice(0, SHOWN_HITS);
  return (
    <Callout tone="danger" className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{t("codebase.ingest.refused")}</span>
        <Badge variant="destructive"><code>{refusal.code}</code></Badge>
      </div>
      <p>{refusal.message}</p>
      {hintKey && <p className="opacity-90">{t(hintKey)}</p>}
      {refusal.code === "secrets_found" && <p className="opacity-90">{t("codebase.ingest.hint.allowMarker")}</p>}
      {shown.length > 0 && (
        <div>
          <div className="mb-1 font-medium">
            {t("codebase.ingest.hits", { count: refusal.hits.length })}
          </div>
          <ul className="max-h-48 space-y-0.5 overflow-y-auto font-mono text-[11px]">
            {shown.map((hit, i) => (
              <li key={`${hit.path}:${hit.line}:${i}`} className="break-all">{hitLabel(hit)}</li>
            ))}
          </ul>
          {refusal.hits.length > shown.length && (
            <p className="mt-1">
              {t("codebase.ingest.moreHits", { count: refusal.hits.length - shown.length })}
            </p>
          )}
        </div>
      )}
    </Callout>
  );
}
