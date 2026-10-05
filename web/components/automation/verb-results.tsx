"use client";

/** What the review / issue / code verbs show in the agent thread.
 *
 *  Kept out of thread.tsx on purpose: that file is shared by every verb, and
 *  these renderers are plain functions of a stored step result. Each one is
 *  compact — a row per run, finding, issue or hit — and every row links to
 *  the page that already shows the thing in full (`/reviews`,
 *  `/pull-requests`, `/issues`, `/repositories`, `/search`) or to the pull
 *  request itself. Nothing here fetches.
 */

import Link from "next/link";
import { ArrowRightIcon } from "lucide-react";

import { isInAppHref } from "@/lib/in-app-href";
import { Badge } from "@/components/ui/badge";

type Translate = (key: string, vars?: Record<string, string | number>) => string;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Result = Record<string, any>;

/** The verbs this file renders, split the way thread.tsx splits them. */
export const VERB_READS = [
  "list_reviews", "get_review_run", "list_issues", "ask_code", "search_code",
] as const;
export const VERB_WRITES = [
  "review_pr", "index_repo", "update_issue",
] as const;
/** Reads whose note is markdown the model (or the answer pipeline) wrote. */
export const VERB_MARKDOWN = [
  "get_review_run", "list_reviews", "ask_code",
] as const;

/** Link labels the server names, and the existing key that spells each. */
const LINK_KEYS: Record<string, string> = {
  reviews: "nav.reviews",
  pull_requests: "prs.navLabel",
  issues: "issues.navLabel",
  repositories: "nav.repositories",
  search: "nav.search",
};

const SEVERITY_VARIANT: Record<string, "destructive" | "warning" | "outline"> = {
  critical: "destructive", error: "destructive", warning: "warning",
};

const CHIP =
  "inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] bg-[var(--color-background)] px-2.5 py-1 text-xs font-medium transition-colors hover:border-[var(--color-brand)]/40 hover:bg-[var(--color-accent)] hover:text-[var(--color-brand)]";
const ROW =
  "flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2 text-sm";
const CODE = "rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]";

function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-[var(--color-muted-foreground)]">{children}</p>;
}

function Links({ links, t }: { links: unknown; t: Translate }) {
  const known = (Array.isArray(links) ? links : []).filter(
    (l: { label?: string; href?: string }) =>
      !!l && typeof l.href === "string" && isInAppHref(l.href) && !!LINK_KEYS[l.label ?? ""],
  ) as { label: string; href: string }[];
  if (known.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {known.map((l) => (
        <Link key={l.href} href={l.href} className={CHIP}>
          {t(LINK_KEYS[l.label])}
          <ArrowRightIcon className="h-3 w-3" />
        </Link>
      ))}
    </div>
  );
}

/** A pull request URL is the provider's, so it is an anchor, and only for
 *  http(s): anything else renders as text. */
function External({ href, children }: { href: unknown; children: React.ReactNode }) {
  if (typeof href !== "string" || !/^https?:\/\//i.test(href)) return <>{children}</>;
  return (
    <a href={href} target="_blank" rel="noreferrer noopener"
       className="underline-offset-2 hover:underline">
      {children}
    </a>
  );
}

function Severity({ name, count }: { name: string; count: number }) {
  if (!count) return null;
  return (
    <Badge variant={SEVERITY_VARIANT[name] ?? "outline"} className="text-[10px]">
      {count} {name}
    </Badge>
  );
}

function RunRow({ run }: { run: Result }) {
  const f = (run.findings ?? {}) as Result;
  return (
    <div className={ROW}>
      <External href={run.pr_url}>
        <code className="text-[12px]">{run.pr_ref ?? run.repo ?? run.run_id}</code>
      </External>
      <Badge variant="outline" className="text-[10px]">{run.verdict || run.status}</Badge>
      {(["critical", "error", "warning", "info"] as const).map((k) => (
        <Severity key={k} name={k} count={Number(f[k] ?? 0)} />
      ))}
      {run.started_at && (
        <span className="ml-auto text-[11px] text-[var(--color-muted-foreground)]">
          {String(run.started_at).slice(0, 16).replace("T", " ")}
        </span>
      )}
    </div>
  );
}

function FindingsTable({ r, t }: { r: Result; t: Translate }) {
  const list: Result[] = Array.isArray(r.findings_list) ? r.findings_list : [];
  if (list.length === 0) return null;
  return (
    <div className="space-y-1">
      {list.map((f, i) => (
        <div key={f.id ?? i} className={ROW}>
          <Badge variant={SEVERITY_VARIANT[f.severity] ?? "outline"} className="text-[10px]">
            {f.severity}
          </Badge>
          <span className="min-w-0 flex-1 wrap-anywhere">{f.title}</span>
          {f.file && (
            <code className={CODE}>{f.file}{f.line ? `:${f.line}` : ""}</code>
          )}
        </div>
      ))}
      {r.truncated && (
        <Muted>{t("automation.verb.showing", {
          shown: Number(r.findings_shown ?? list.length), total: Number(r.findings_total ?? list.length),
        })}</Muted>
      )}
    </div>
  );
}

function IssueRows({ r, t }: { r: Result; t: Translate }) {
  const list: Result[] = Array.isArray(r.issues) ? r.issues : [];
  if (list.length === 0) return <Muted>{t("automation.verb.noIssues")}</Muted>;
  return (
    <div className="space-y-1">
      {list.map((i) => (
        <div key={i.id} className={ROW}>
          <Badge variant={SEVERITY_VARIANT[i.severity] ?? "outline"} className="text-[10px]">
            {i.severity}
          </Badge>
          <span className="min-w-0 flex-1 wrap-anywhere">{i.title}</span>
          <Badge variant="outline" className="text-[10px]">{i.status}</Badge>
          {i.file && <code className={CODE}>{i.file}{i.line ? `:${i.line}` : ""}</code>}
          {i.repo && i.pr_number ? (
            <External href={i.pr_url}>
              <code className={CODE}>{i.repo}#{i.pr_number}</code>
            </External>
          ) : null}
        </div>
      ))}
      {Number(r.total) > list.length && (
        <Muted>{t("automation.verb.showing", { shown: list.length, total: Number(r.total) })}</Muted>
      )}
    </div>
  );
}

function Sources({ files, t }: { files: string[]; t: Translate }) {
  if (files.length === 0) return null;
  return (
    <div className="space-y-1">
      <Muted>{t("automation.verb.sources")}</Muted>
      <div className="flex flex-wrap gap-1">
        {files.map((f) => <code key={f} className={CODE}>{f}</code>)}
      </div>
    </div>
  );
}

function SearchRows({ r, t }: { r: Result; t: Translate }) {
  if (r.kind === "owner") {
    const authors: Result[] = Array.isArray(r.top_authors) ? r.top_authors : [];
    const codeowners: unknown[] = Array.isArray(r.codeowners) ? r.codeowners : [];
    if (!r.primary_owner && authors.length === 0 && codeowners.length === 0) {
      return <Muted>{r.note ?? t("automation.verb.noMatches")}</Muted>;
    }
    return (
      <div className={ROW}>
        <code className={CODE}>{r.path}</code>
        <span className="text-xs text-[var(--color-muted-foreground)]">
          {t("automation.verb.owner")}
        </span>
        {r.primary_owner && (
          <Badge variant="outline" className="text-[10px]">
            {typeof r.primary_owner === "string"
              ? r.primary_owner
              : String(r.primary_owner.name ?? r.primary_owner.email ?? "")}
          </Badge>
        )}
        {authors.map((a, i) => (
          <Badge key={i} variant="outline" className="text-[10px]">
            {String(a.name ?? a.email ?? a.author ?? "")}
          </Badge>
        ))}
        {codeowners.map((c, i) => (
          <Badge key={`c${i}`} variant="outline" className="text-[10px]">{String(c)}</Badge>
        ))}
      </div>
    );
  }
  if (r.kind === "architecture") {
    // The summary is markdown the note already carries only when the model
    // explained it; show it plainly, bounded, so it reads the same either way.
    return r.summary_md
      ? <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-[var(--color-border)] p-3 text-xs">{r.summary_md}</pre>
      : <Muted>{r.note ?? t("automation.verb.noMatches")}</Muted>;
  }
  const rows: Result[] = r.kind === "usages"
    ? (Array.isArray(r.usages) ? r.usages : [])
    : (Array.isArray(r.symbols) ? r.symbols : []);
  const notes: Result[] = Array.isArray(r.notes) ? r.notes : [];
  if (rows.length === 0 && notes.length === 0) {
    return <Muted>{t("automation.verb.noMatches")}</Muted>;
  }
  return (
    <div className="space-y-1">
      {r.kind === "usages" && (
        <Muted>{t("automation.verb.usedBy")} <code className={CODE}>{r.symbol}</code></Muted>
      )}
      {rows.map((s, i) => (
        <div key={i} className={ROW}>
          <code className="text-[12px]">{s.name}</code>
          {s.kind && <Badge variant="outline" className="text-[10px]">{s.kind}</Badge>}
          {s.file && (
            <External href={s.web_url}>
              <code className={CODE}>{s.file}{s.line ? `:${s.line}` : ""}</code>
            </External>
          )}
          {s.repo_slug && <span className="ml-auto text-[11px] text-[var(--color-muted-foreground)]">{s.repo_slug}</span>}
        </div>
      ))}
      {notes.map((n, i) => (
        <div key={`n${i}`} className={ROW}>
          <span className="min-w-0 flex-1 wrap-anywhere">{n.title || n.path}</span>
          {n.path && <code className={CODE}>{n.path}</code>}
        </div>
      ))}
    </div>
  );
}

/** The body of an answered read step, or null when this file does not own it. */
export function VerbAnswer({
  action, result, t,
}: { action: string; result: Result; t: Translate }) {
  const r = result ?? {};
  switch (action) {
    case "list_reviews": {
      const runs: Result[] = Array.isArray(r.runs) ? r.runs : [];
      return (
        <div className="space-y-1">
          {runs.length === 0 ? <Muted>{t("automation.verb.noReviews")}</Muted> : runs.map((run) => (
            <RunRow key={run.run_id} run={run} />
          ))}
          <Links links={r.links} t={t} />
        </div>
      );
    }
    case "get_review_run":
      return (
        <div className="space-y-2">
          <RunRow run={r} />
          <FindingsTable r={r} t={t} />
          <Links links={r.links} t={t} />
        </div>
      );
    case "list_issues":
      return (
        <div className="space-y-2">
          <IssueRows r={r} t={t} />
          <Links links={r.links} t={t} />
        </div>
      );
    case "ask_code":
      // The answer is the note above; what is left is where it came from.
      return (
        <div className="space-y-2">
          <Sources files={Array.isArray(r.files) ? r.files.map(String) : []} t={t} />
          <Links links={r.links} t={t} />
        </div>
      );
    case "search_code":
      return (
        <div className="space-y-2">
          <SearchRows r={r} t={t} />
          <Links links={r.links} t={t} />
        </div>
      );
    default:
      return null;
  }
}

/** The change a planned `review_pr` / `update_issue` / `index_repo` card shows. */
export type VerbPreviewData =
  | { kind: "review_pr"; repo: string; number: number | null; all_open: boolean;
      numbers?: number[] | null; post_comments: boolean }
  | { kind: "issue"; ids: string[]; status: string; repo: string | null };

export function VerbPreview({ preview, t }: { preview: VerbPreviewData; t: Translate }) {
  if (preview.kind === "review_pr") {
    return (
      <p className="mb-1 text-xs text-[var(--color-muted-foreground)]">
        <code className={`mr-1 ${CODE}`}>{preview.repo}</code>
        {preview.all_open || !preview.number
          ? t("automation.verb.reviewAll")
          : t("automation.verb.reviewOne", { number: preview.number })}
        {" · "}
        {preview.post_comments ? t("automation.verb.postsComments") : t("automation.verb.noPost")}
      </p>
    );
  }
  return (
    <p className="mb-1 text-xs text-[var(--color-muted-foreground)]">
      {t("automation.verb.setIssues", { count: preview.ids.length })}{" "}
      <Badge variant="outline" className="text-[10px]">{preview.status}</Badge>
      {preview.repo && <code className={`ml-1 ${CODE}`}>{preview.repo}</code>}
    </p>
  );
}

/** What a confirmed write did, in detail: skipped items and the links. */
export function VerbOutcome({
  action, result, t,
}: { action: string; result: Result; t: Translate }) {
  const r = result ?? {};
  const skipped: Result[] = Array.isArray(r.skipped) ? r.skipped : [];
  const queued: Result[] = Array.isArray(r.queued) ? r.queued : [];
  const done: Result[] = Array.isArray(r.updated) ? r.updated : [];
  const summary = action === "review_pr"
    ? t("automation.verb.reviewsQueued", { count: queued.length })
    : action === "index_repo"
      ? t("automation.verb.indexQueued", { count: queued.length })
      : t("automation.verb.issuesUpdated", { count: Number(r.count ?? done.length) });
  return (
    <div className="space-y-1.5">
      <Muted>{summary}</Muted>
      {skipped.map((s, i) => (
        <Muted key={i}>
          <code className={CODE}>{String(s.repo ?? s.id ?? s.number ?? "")}</code>{" "}
          {String(s.reason ?? "")}
        </Muted>
      ))}
      <Links links={r.links} t={t} />
    </div>
  );
}
