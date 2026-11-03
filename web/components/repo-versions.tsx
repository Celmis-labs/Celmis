"use client";

/**
 * Upload a new version of an uploaded repository, and see the versions it has had.
 *
 * The repository keeps its name, so everything that refers to it (tokens, shares,
 * projects, chats) needs no action; the server unpacks and indexes the archive
 * before it replaces the served one, so a bad archive leaves the old version
 * serving and this dialog says why.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, UploadIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  repoVersionsApi, uploadRepoVersion,
  type RepoOut, type RepoVersion, type RepoVersionResult,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { formatBytes } from "@/lib/codebase";
import { useToken } from "@/lib/use-token";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const PHASES = ["queued", "starting", "unpacking", "comparing", "indexing", "swapping", "cleaning"];

function Changes({ v }: { v: Pick<RepoVersion, "added" | "removed" | "changed"> }) {
  const t = useT();
  if (v.added == null && v.removed == null && v.changed == null) return null;
  return (
    <span>
      {t("repoVersions.changes", {
        added: v.added ?? 0, removed: v.removed ?? 0, changed: v.changed ?? 0,
      })}
    </span>
  );
}

function VersionLine({ v }: { v: RepoVersion }) {
  const t = useT();
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-xs">
      <span className="font-medium">v{v.version}</span>
      <span>{formatDateTime(v.uploaded_at)}</span>
      {v.uploaded_by && <span className="text-[var(--color-muted-foreground)]">{v.uploaded_by}</span>}
      <span className="text-[var(--color-muted-foreground)]">
        {v.filename} · {t("repoVersions.fileCount", { n: v.files })} · {formatBytes(v.size)}
      </span>
      <Changes v={v} />
    </li>
  );
}

export function RepoVersionButton({ repo, onDone }: { repo: RepoOut; onDone: () => void }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [phase, setPhase] = useState<string | null>(null);
  const [abort, setAbort] = useState<AbortController | null>(null);
  const [result, setResult] = useState<RepoVersionResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const versions = useQuery({
    queryKey: ["repo-versions", repo.slug],
    queryFn: () => repoVersionsApi.list(token!, repo.slug),
    enabled: !!token && open,
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const ctl = new AbortController();
      setAbort(ctl);
      setResult(null);
      setFailure(null);
      setProgress(0);
      setPhase(null);
      try {
        return await uploadRepoVersion(token!, repo.slug, file, {
          onProgress: setProgress, onPhase: setPhase, signal: ctl.signal,
        });
      } finally {
        setAbort(null);
        setProgress(null);
        setPhase(null);
      }
    },
    onSuccess: async (r) => {
      setResult(r);
      toast.success(t("repoVersions.done", { version: r.version.version }));
      await qc.invalidateQueries({ queryKey: ["repo-versions", repo.slug] });
      onDone();
    },
    onError: (e: Error) => {
      if (e.name !== "AbortError") setFailure(e.message);
    },
  });

  const current = versions.data?.current ?? null;
  const busy = upload.isPending;
  const phaseLabel = phase
    ? t(`repoVersions.phase.${PHASES.includes(phase) ? phase : "working"}`)
    : t("repoVersions.phase.uploading");

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) setOpen(v); }}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title={t("repoVersions.title")}
          aria-label={t("repoVersions.title")}>
          <UploadIcon className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("repoVersions.title")}</DialogTitle>
          <DialogDescription>{t("repoVersions.intro", { name: repo.display_name || repo.full_name })}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <div className="text-sm font-medium">
            {current
              ? t("repoVersions.current", { version: current.version })
              : t("repoVersions.none")}
          </div>
          {versions.data && versions.data.history.length > 0 && (
            <ul className="flex max-h-40 flex-col gap-1 overflow-y-auto" aria-label={t("repoVersions.history")}>
              {versions.data.history.slice(0, 8).map((v) => <VersionLine key={v.version} v={v} />)}
            </ul>
          )}
        </div>

        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            const file = new FormData(e.currentTarget).get("file");
            if (file instanceof File && file.size > 0) upload.mutate(file);
          }}
        >
          <div>
            <Label htmlFor="version-file">{t("repoVersions.file")}</Label>
            <Input id="version-file" name="file" type="file" required disabled={busy}
              accept=".zip,.tar.gz,.tgz" />
          </div>
          {busy && (
            <div className="flex flex-col gap-1">
              <div role="progressbar" aria-valuenow={Math.round((progress ?? 1) * 100)}
                aria-valuemin={0} aria-valuemax={100}
                className="h-2 w-full overflow-hidden rounded bg-[var(--color-muted)]">
                <div className="h-full bg-[var(--color-primary)] transition-all"
                  style={{ width: `${Math.round((progress ?? 1) * 100)}%` }} />
              </div>
              <p className="flex items-center gap-1 text-xs text-[var(--color-muted-foreground)]">
                <Loader2Icon className="h-3 w-3 animate-spin" /> {phaseLabel}
              </p>
            </div>
          )}
          {failure && (
            <Callout tone="danger">{t("repoVersions.failed", { message: failure })}</Callout>
          )}
          {result && (
            <Callout tone="success">
              <div>{t("repoVersions.done", { version: result.version.version })}</div>
              <div className="text-xs">
                {t("repoVersions.summary", { files: result.files_indexed, symbols: result.symbols })}
                {" · "}<Changes v={result} />
              </div>
              {result.notes_removed > 0 && (
                <div className="text-xs">{t("repoVersions.notesRemoved", { n: result.notes_removed })}</div>
              )}
            </Callout>
          )}
          <DialogFooter>
            {abort ? (
              <Button type="button" variant="outline" onClick={() => abort.abort()}>
                {t("repositories.archiveCancel")}
              </Button>
            ) : (
              <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
                {t("repoVersions.close")}
              </Button>
            )}
            <Button type="submit" disabled={busy || !token}>
              {busy ? t("repoVersions.uploading") : t("repoVersions.upload")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
