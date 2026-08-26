"use client";

/**
 * The onboarding step that shows something instead of asking for something.
 *
 * The six before it are configuration: a workspace, an LLM key, a git
 * connection, an index, a vault, an agent. Ten minutes of setup before the
 * product has demonstrated anything at all — the worst possible order for a
 * tool whose strength is a finding you can check in five seconds.
 *
 * So this one runs the dependency audit and puts ONE finding on screen. Which
 * finding matters: deliberately a PROVEN one — a lock file that disagrees with
 * its manifest, a package that runs a script at install, a dependency pulled
 * from outside the registry. Those need no defending. The reader looks at the
 * file, the line and the line's text, and agrees or disagrees immediately.
 *
 * A model's finding in this position would ask for trust that has not been
 * earned yet, on the very first thing anybody sees.
 *
 * Beside it the SBOM: the artefact procurement asks for by name, one click
 * from the end of setup rather than behind an audit nobody has run.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon, ArrowRightIcon, PackageSearchIcon, PlayIcon,
  RefreshCwIcon, ShieldCheckIcon,
} from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { depsApi, downloadWithAuth, type HygieneItem } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/** Which finding to lead with.
 *
 *  Ordered by how little explaining it needs, not by severity. A lock file
 *  that disagrees with its manifest is understood by anybody who has ever run
 *  an install; a CVE identifier is a lookup. Severity decides what to fix
 *  first; this decides what to SHOW first — different questions.
 *
 *  Every kind here is deterministic. A finding with no excerpt still qualifies
 *  (npm records `hasInstallScript` as a boolean and never the script body) —
 *  the file and the check are the proof in that case. */
/**
 *  `suspect_name` is deliberately NOT here. It is edit-distance ≤1 against a
 *  hard-coded list of ~150 popular names, and its own message ends "confirm
 *  this is the package you meant" — a check that asks the reader to confirm is
 *  the opposite of one that proves. Deterministic and correct are different
 *  properties: the computation is exact, the conclusion is a guess, and it is
 *  the conclusion the badge is about. Putting a typosquat suspicion under
 *  "Proven" on the first screen a new user sees would spend the credibility
 *  this step exists to build.
 */
const PROVEN_KINDS = ["lock_drift", "install_script", "non_registry"];

export function pickProven(items: HygieneItem[]): HygieneItem | null {
  for (const kind of PROVEN_KINDS) {
    // An item carrying its own line and text is the better demonstration, so
    // prefer it over another of the same kind that has neither.
    const of = items.filter((i) => i.kind === kind);
    const withEvidence = of.find((i) => i.excerpt);
    if (withEvidence) return withEvidence;
    if (of.length) return of[0];
  }
  return null;
}

export function FirstProofStep({ hasRepo }: { hasRepo: boolean }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();

  const latest = useQuery({
    queryKey: ["deps-latest-onboarding"],
    queryFn: () => depsApi.latest(token!),
    enabled: !!token,
    // A run started here finishes minutes later, and polling is the only
    // signal. It stops the moment the run is terminal.
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "running" || s === "queued" ? 5000 : false;
    },
  });

  const run = latest.data;
  const done = run?.status === "done";
  const running = run?.status === "running" || run?.status === "queued";
  // A failed run rendered exactly like "never started": the button came back
  // with no explanation, so the one message that tells you how to recover — a
  // stuck queue job needs cancelling — was never shown, and clicking again
  // reproduced it forever.
  const failed = run?.status === "error";

  const start = useMutation({
    mutationFn: () => depsApi.startAudit(token!),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["deps-latest-onboarding"] });
      // The endpoint answers 202 even when it refused to queue anything: it
      // returns the run with status "error" and an actionable reason. Toasting
      // success on that told the user to go and wait for an audit that was
      // never enqueued.
      if (r?.status === "error") toast.error(r.error || t("onboarding.proof.failed"));
      else toast.success(t("onboarding.proof.started"));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const download = async (url: string, name: string) => {
    try {
      await downloadWithAuth(url, name, token);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const items = run?.summary?.hygiene?.items ?? [];
  const proven = done ? pickProven(items) : null;

  // "Nothing to flag" must mean the checks ran and found nothing — not that
  // they never ran. The auditor writes status "done" even when every repo
  // failed to clone, so an empty hygiene list can equally mean "your supply
  // chain is clean" or "nothing was opened". Told the first, a first-run user
  // gets a clean bill of health and an empty SBOM off a run that read no files.
  const summary = run?.summary as {
    repos_scanned?: number; repos_skipped?: string[];
  } | undefined;
  const scanned = summary?.repos_scanned ?? 0;
  const skipped = summary?.repos_skipped ?? [];
  const nothingScanned = done && scanned === 0;

  return (
    <div className="space-y-3">
      {!hasRepo && (
        <p className="text-sm text-[var(--color-muted-foreground)]">
          {t("onboarding.proof.needRepo")}
        </p>
      )}

      {hasRepo && !done && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={start.isPending || running}
                  onClick={() => start.mutate()}>
            {running || start.isPending
              ? <RefreshCwIcon className="mr-1 h-3.5 w-3.5 animate-spin" />
              : <PlayIcon className="mr-1 h-3.5 w-3.5" />}
            {running
              ? t("onboarding.proof.running")
              : failed ? t("onboarding.proof.retry") : t("onboarding.proof.run")}
          </Button>
          <span className="text-xs text-[var(--color-muted-foreground)]">
            {t("onboarding.proof.noKeyNeeded")}
          </span>
        </div>
      )}

      {failed && (
        <div className="rounded-lg border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 p-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangleIcon className="h-4 w-4 text-[var(--color-destructive)]" />
            {t("onboarding.proof.failedTitle")}
          </div>
          {/* run.error carries the recovery instruction — "cancel it in
              Operations → Job queue and retry" — which was the only way out of
              the stuck-queue state and was never rendered anywhere. */}
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            {run?.error || t("onboarding.proof.failed")}
          </p>
        </div>
      )}

      {proven && (
        <div className="rounded-lg border border-[var(--color-brand)]/40 bg-[var(--color-brand-muted)]/30 p-3">
          <div className="mb-1.5 flex flex-wrap items-center gap-2">
            <Badge variant="success" className="text-[10px]">
              {t("onboarding.proof.provenBadge")}
            </Badge>
            <span className="text-sm font-medium">
              {t(`deps.hygieneKind.${proven.kind}`)}
            </span>
            <code className="text-xs">{proven.package}</code>
            {proven.repo && (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                · {proven.repo}
              </span>
            )}
          </div>
          <p className="text-sm">{proven.detail}</p>
          {/* The file, the line, and the line's text. This is the whole
              point: nothing here has to be taken on trust. */}
          <p className="mt-1.5 font-mono text-[11px] text-[var(--color-muted-foreground)]">
            {proven.location}{proven.line ? `:${proven.line}` : ""}
          </p>
          {proven.excerpt && (
            <code className="mt-1 block overflow-x-auto whitespace-pre rounded bg-[var(--color-muted)]/40 p-2 text-[11px]">
              {proven.excerpt}
            </code>
          )}
          {/* Two wordings, because one of them would be a small lie half the
              time. npm's lock records `hasInstallScript` as a boolean and
              never the script body, so an install-script finding has a file
              and no line — and "open the file at that line" then points at
              something that is not on screen, in the one card whose argument
              is that you do not have to take its word for anything. */}
          <p className="mt-2 text-xs text-[var(--color-muted-foreground)]">
            {proven.line
              ? t("onboarding.proof.whyProven")
              : t("onboarding.proof.whyProvenNoLine")}
          </p>
        </div>
      )}

      {nothingScanned && (
        // The opposite of a clean bill: nothing was read, so nothing is known.
        // A skip here almost always means the clone failed, which is why the
        // repositories are named — the reader needs to know which token to fix.
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/20 p-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangleIcon className="h-4 w-4 text-[var(--color-muted-foreground)]" />
            {t("onboarding.proof.nothingScanned")}
          </div>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            {t("onboarding.proof.nothingScannedHint")}
          </p>
          {skipped.length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-[var(--color-muted-foreground)]">
              {skipped.slice(0, 5).join(", ")}
              {skipped.length > 5 ? ` +${skipped.length - 5}` : ""}
            </p>
          )}
        </div>
      )}

      {done && !proven && !nothingScanned && (
        // Not a failure, and it must not read like one: a clean supply chain
        // is the good outcome, and the SBOM below is still the deliverable.
        <p className="text-sm text-[var(--color-muted-foreground)]">
          {t("onboarding.proof.allClean")}
        </p>
      )}

      {done && run && !nothingScanned && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {/* Buttons, not links. These were `<a href download>`, which is a
              browser navigation: it carries cookies and no Authorization
              header, and every export endpoint reads that header and nothing
              else — so "Download SBOM" saved a file containing
              {"detail":"Missing or invalid Authorization header"}. */}
          <Button size="sm" variant="outline"
                  onClick={() => download(depsApi.sbomUrl(run.id),
                                          `celmis-sbom-${run.id}.cdx.json`)}>
            <PackageSearchIcon className="mr-1 h-3.5 w-3.5" />
            {t("deps.downloadSbom")}
          </Button>
          <Button size="sm" variant="outline"
                  onClick={() => download(depsApi.evidenceUrl(run.id),
                                          `celmis-evidence-${run.id}.zip`)}>
            <ShieldCheckIcon className="mr-1 h-3.5 w-3.5" />
            {t("deps.downloadEvidence")}
          </Button>
          <Link href="/dependencies">
            <Button size="sm" variant="ghost">
              {t("onboarding.proof.seeAll")}
              <ArrowRightIcon className="ml-1 h-3.5 w-3.5" />
            </Button>
          </Link>
        </div>
      )}
    </div>
  );
}
