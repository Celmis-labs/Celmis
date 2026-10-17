"use client";

/**
 * One file of a pack, opened in place.
 *
 * Markdown is rendered (the front matter, which the file format requires for
 * a skill, is split off and shown apart so it does not read as a table of
 * garbage) with its images as text — a pack is somebody else's writing, and
 * opening it must not call the addresses it names; anything else is shown as it is. The source is always one click
 * away, because what an agent receives is the source.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { codebaseApi } from "@/lib/api";
import { formatBytes, isMarkdownPath, splitFrontMatter } from "@/lib/codebase";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Markdown } from "@/components/markdown";
import { CopyButton } from "@/components/codebase/copy";
import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SkeletonRows } from "@/components/ui/skeleton";

/** Past this, rendering markdown stalls the tab; the source is shown instead. */
const RENDER_LIMIT = 200_000;

export function ItemViewer({
  packId,
  itemId,
  onClose,
}: {
  packId: string;
  /** The file to show; null keeps the dialog closed. */
  itemId: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const token = useToken();
  const [view, setView] = useState<"rendered" | "source">("rendered");
  const item = useQuery({
    queryKey: ["codebase", "item", packId, itemId],
    queryFn: () => codebaseApi.item(token!, packId, itemId!),
    enabled: !!token && !!itemId,
  });
  const data = item.data;
  const markdown = !!data && isMarkdownPath(data.path) && data.content.length <= RENDER_LIMIT;
  const parts = data && markdown ? splitFrontMatter(data.content) : null;

  return (
    <Dialog open={!!itemId} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="break-all font-mono text-sm">
            {data?.path ?? t("common.loading")}
          </DialogTitle>
          <DialogDescription>{data?.description || data?.name || t("codebase.viewer.desc")}</DialogDescription>
        </DialogHeader>

        {item.isError && <Callout tone="danger">{(item.error as Error).message}</Callout>}
        {item.isPending && !item.isError && <SkeletonRows rows={4} />}

        {data && (
          <div className="min-w-0 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
              <Badge variant="outline">{t(`codebase.kind.${data.kind}`)}</Badge>
              <span>{t("codebase.viewer.version", { number: data.version })}</span>
              <span>{formatBytes(data.size_bytes)}</span>
              <code title={data.sha}>{data.sha.slice(0, 8)}</code>
              <span className="ml-auto flex items-center gap-2">
                {markdown && (
                  <SegmentedControl
                    size="sm"
                    semantics="radio"
                    label={t("codebase.viewer.view")}
                    value={view}
                    onValueChange={setView}
                    segments={[
                      { value: "rendered", label: t("codebase.viewer.rendered") },
                      { value: "source", label: t("codebase.viewer.source") },
                    ]}
                  />
                )}
                <CopyButton size="xs" text={data.content} />
              </span>
            </div>

            {markdown && view === "rendered" && parts ? (
              <div className="max-h-[60dvh] space-y-3 overflow-y-auto rounded-lg border border-[var(--color-border)] p-3">
                {parts.front !== null && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-[var(--color-muted-foreground)]">
                      {t("codebase.viewer.frontMatter")}
                    </summary>
                    <pre className="mt-1 overflow-x-auto rounded-md bg-[var(--color-muted)]/40 p-2 text-[11px]">
                      <code>{parts.front}</code>
                    </pre>
                  </details>
                )}
                <Markdown text={parts.body} images="alt" />
              </div>
            ) : (
              <>
                {isMarkdownPath(data.path) && !markdown && (
                  <Callout tone="info">{t("codebase.viewer.tooLarge")}</Callout>
                )}
                <pre className="max-h-[60dvh] overflow-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3 text-[12px] leading-relaxed">
                  <code>{data.content}</code>
                </pre>
              </>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
