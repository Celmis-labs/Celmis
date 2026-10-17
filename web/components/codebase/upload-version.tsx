"use client";

/**
 * Upload a new version of a pack: a .zip, or a whole folder.
 *
 * The browser sends what was picked; the server decides what a pack is made of
 * (it ignores what is not text, refuses secrets and malformed skills) and
 * answers with a structured reason, which is shown as it came. This dialog
 * only keeps junk that no pack wants (`.git`, `node_modules`) out of the
 * request, and stops a pick that cannot fit in one upload before it is sent.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, FolderUpIcon, UploadIcon } from "lucide-react";
import { toast } from "sonner";

import { codebaseApi, relativePathOf, type PackUploaded } from "@/lib/api";
import { formatBytes, hitLabel, keptByServer, skippedLocally, UPLOAD_LIMITS } from "@/lib/codebase";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { IngestError } from "@/components/codebase/ingest-error";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { InlineHelp } from "@/components/ui/inline-help";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Textarea } from "@/components/ui/textarea";

const NOTE_MAX = 500;
/** The ignored list can be long; the dialog shows the first of it. */
const IGNORED_SHOWN = 100;

/** `webkitdirectory` is not in React's typings; the attribute is what makes
 *  the picker choose a folder and hand back every file under it. */
const FOLDER_ATTRS = { webkitdirectory: "", directory: "" } as unknown as
  React.InputHTMLAttributes<HTMLInputElement>;

type Mode = "zip" | "folder";
/** `files` is what is sent; `kept` is how many of them the server keeps as the
 *  pack's files (text formats), the number its limit counts. */
type Folder = { files: File[]; kept: number; skipped: number; name: string };

export function UploadVersionDialog({
  packId,
  open,
  onOpenChange,
}: {
  packId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("codebase.upload.title")}</DialogTitle>
          <DialogDescription>{t("codebase.upload.desc")}</DialogDescription>
        </DialogHeader>
        {/* Rendered only while open, so closing the dialog drops the picked
            files, the error and the progress with it. */}
        <UploadForm packId={packId} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

function UploadForm({ packId, onClose }: { packId: string; onClose: () => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>("zip");
  const [archive, setArchive] = useState<File | null>(null);
  const [folder, setFolder] = useState<Folder | null>(null);
  const [note, setNote] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [done, setDone] = useState<PackUploaded | null>(null);
  const controller = useRef<AbortController | null>(null);

  // Closing the dialog mid-upload cancels the request rather than leaving it
  // running behind a screen that no longer shows it.
  useEffect(() => () => controller.current?.abort(), []);

  const upload = useMutation({
    mutationFn: () => {
      controller.current = new AbortController();
      setProgress(0);
      return codebaseApi.upload(
        token!, packId,
        mode === "zip" ? { archive: archive!, note: note.trim() } : { files: folder!.files, note: note.trim() },
        { onProgress: setProgress, signal: controller.current.signal },
      );
    },
    onSuccess: (r) => {
      setDone(r);
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.upload.toast", { number: r.version.number }));
    },
    onSettled: () => setProgress(null),
  });

  const bytes = mode === "zip"
    ? archive?.size ?? 0
    : (folder?.files ?? []).reduce((sum, f) => sum + f.size, 0);
  const picked = mode === "zip" ? !!archive : (folder?.files.length ?? 0) > 0;
  const tooBig = bytes > UPLOAD_LIMITS.archiveBytes;
  const tooMany = mode === "folder" && (folder?.kept ?? 0) > UPLOAD_LIMITS.files;
  const cancelled = upload.error instanceof DOMException && upload.error.name === "AbortError";

  const chooseMode = (next: Mode) => {
    if (upload.isPending) return;
    setMode(next);
    upload.reset();
  };

  const onFolder = (list: FileList | null) => {
    upload.reset();
    const all = Array.from(list ?? []);
    const files = all.filter((f) => !skippedLocally(relativePathOf(f)));
    const first = relativePathOf(all[0] ?? ({ name: "" } as File));
    setFolder(all.length === 0 ? null : {
      files,
      kept: files.filter((f) => keptByServer(relativePathOf(f))).length,
      skipped: all.length - files.length,
      name: first.includes("/") ? first.split("/")[0] : "",
    });
  };

  if (done) {
    // The server lists at most 200 ignored paths and says how many there were.
    const ignoredTotal = Math.max(done.ignored_total ?? 0, done.ignored.length);
    return (
      <div className="min-w-0 space-y-3">
        <Callout tone="success" className="flex items-start gap-2">
          <CheckCircle2Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>
            {t("codebase.upload.done", {
              number: done.version.number, files: done.version.file_count, skills: done.skills.length,
            })}
          </span>
        </Callout>
        {done.skills.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {done.skills.map((name) => <Badge key={name} variant="brand"><code>{name}</code></Badge>)}
          </div>
        )}
        {ignoredTotal > 0 && (
          <details className="text-xs">
            <summary className="cursor-pointer text-[var(--color-muted-foreground)]">
              {t("codebase.upload.ignored", { count: ignoredTotal })}
            </summary>
            <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px]">
              {done.ignored.slice(0, IGNORED_SHOWN).map((path) => <li key={path} className="break-all">{path}</li>)}
            </ul>
            {ignoredTotal > IGNORED_SHOWN && (
              <p className="mt-1">{t("codebase.upload.ignoredMore", { count: ignoredTotal - IGNORED_SHOWN })}</p>
            )}
          </details>
        )}
        {(done.allowed?.length ?? 0) > 0 && (
          <details className="text-xs" open>
            <summary className="cursor-pointer text-[var(--color-muted-foreground)]">
              {t("codebase.upload.allowed", { count: done.allowed?.length ?? 0 })}
            </summary>
            <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px]">
              {done.allowed?.slice(0, IGNORED_SHOWN).map((hit, i) => (
                <li key={`${hit.path}:${hit.line}:${i}`} className="break-all">{hitLabel({ path: hit.path, line: hit.line, label: hit.rule })}</li>
              ))}
            </ul>
          </details>
        )}
        <DialogFooter>
          <Button onClick={onClose}>{t("codebase.upload.close")}</Button>
        </DialogFooter>
      </div>
    );
  }

  return (
    <form
      className="min-w-0 space-y-4"
      onSubmit={(e) => { e.preventDefault(); if (picked && !tooBig && token) upload.mutate(); }}
    >
      <SegmentedControl
        value={mode}
        onValueChange={chooseMode}
        label={t("codebase.upload.source")}
        segments={[
          { value: "zip", label: t("codebase.upload.zip") },
          { value: "folder", label: t("codebase.upload.folder") },
        ]}
      />

      {mode === "zip" ? (
        <div className="space-y-1.5">
          <Label htmlFor="pack-archive">{t("codebase.upload.zipLabel")}</Label>
          <Input
            id="pack-archive" type="file" accept=".zip,application/zip"
            disabled={upload.isPending}
            onChange={(e) => { upload.reset(); setArchive(e.target.files?.[0] ?? null); }}
          />
        </div>
      ) : (
        <div className="space-y-1.5">
          <Label htmlFor="pack-folder">{t("codebase.upload.folderLabel")}</Label>
          <Input
            id="pack-folder" type="file" multiple {...FOLDER_ATTRS}
            disabled={upload.isPending}
            onChange={(e) => onFolder(e.target.files)}
          />
          {folder && (
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("codebase.upload.folderPicked", {
                count: folder.kept, size: formatBytes(bytes),
              })}
              {folder.skipped > 0 && ` ${t("codebase.upload.folderSkipped", { count: folder.skipped })}`}
            </p>
          )}
        </div>
      )}
      {mode === "zip" && archive && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {archive.name} · {formatBytes(archive.size)}
        </p>
      )}

      {tooBig && (
        <Callout tone="warning">
          {t("codebase.upload.tooBig", { size: formatBytes(bytes), limit: formatBytes(UPLOAD_LIMITS.archiveBytes) })}
        </Callout>
      )}
      {tooMany && !tooBig && (
        <Callout tone="warning">{t("codebase.upload.tooMany", { limit: UPLOAD_LIMITS.files })}</Callout>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="pack-note">{t("codebase.upload.note")}</Label>
        <Textarea
          id="pack-note" rows={2} maxLength={NOTE_MAX} value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("codebase.upload.notePlaceholder")}
          disabled={upload.isPending}
        />
      </div>

      <InlineHelp question={t("codebase.upload.helpQuestion")}>
        <div className="space-y-2">
          <p>{t("codebase.upload.helpSkills")}</p>
          <p>{t("codebase.upload.helpKinds")}</p>
          <p>
            {t("codebase.upload.helpLimits", {
              files: UPLOAD_LIMITS.files,
              file: formatBytes(UPLOAD_LIMITS.fileBytes),
              total: formatBytes(UPLOAD_LIMITS.totalBytes),
              body: formatBytes(UPLOAD_LIMITS.archiveBytes),
            })}
          </p>
          <p>{t("codebase.upload.helpRefused")}</p>
        </div>
      </InlineHelp>

      {progress !== null && (
        <div className="space-y-1">
          <div
            role="progressbar" aria-label={t("codebase.upload.progress")}
            aria-valuenow={Math.round(progress * 100)} aria-valuemin={0} aria-valuemax={100}
            className="h-2 w-full overflow-hidden rounded bg-[var(--color-muted)]"
          >
            <div
              className="h-full bg-[var(--color-primary)] transition-all"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {progress >= 1 ? t("codebase.upload.processing") : t("codebase.upload.sending", { percent: Math.round(progress * 100) })}
          </p>
        </div>
      )}

      {upload.isError && !cancelled && <IngestError error={upload.error} />}
      {cancelled && <Callout tone="info">{t("codebase.upload.cancelled")}</Callout>}

      <DialogFooter>
        {upload.isPending ? (
          <Button type="button" variant="outline" onClick={() => controller.current?.abort()}>
            {t("common.cancel")}
          </Button>
        ) : (
          <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
        )}
        <Button type="submit" disabled={!picked || tooBig || tooMany || !token} loading={upload.isPending}>
          {mode === "zip" ? <UploadIcon /> : <FolderUpIcon />}
          {upload.isPending ? t("codebase.upload.uploading") : t("codebase.upload.submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}
