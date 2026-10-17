"use client";

/**
 * A pack's versions: what was uploaded when and by whom, which one agents are
 * reading, and what changed between two of them. Rolling back is activating an
 * older version; nothing is lost, the server keeps the newest twenty.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DownloadIcon, GitCompareIcon, HistoryIcon, Undo2Icon } from "lucide-react";
import { toast } from "sonner";

import { codebaseApi, type PackDetail, type PackVersionRow } from "@/lib/api";
import { diffPath, formatBytes } from "@/lib/codebase";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { QueryState } from "@/components/ui/query-state";
import { Select } from "@/components/ui/select";
import { SkeletonRows } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

export function VersionsTab({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [compare, setCompare] = useState<number | null>(null);
  const [downloading, setDownloading] = useState<number | null>(null);

  const versions = useQuery({
    queryKey: ["codebase", "versions", pack.id],
    queryFn: () => codebaseApi.versions(token!, pack.id),
    enabled: !!token,
  });

  const activate = useMutation({
    mutationFn: (number: number) => codebaseApi.activate(token!, pack.id, number),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.versions.activated"));
    },
    onError: (e) => toast.error(t("codebase.versions.activateFailed", { message: (e as Error).message })),
  });

  const download = async (number: number) => {
    setDownloading(number);
    try {
      await codebaseApi.downloadArchive(token!, pack.id, number, `${pack.slug}-v${number}.zip`);
    } catch (e) {
      toast.error(t("codebase.versions.downloadFailed", { message: (e as Error).message }));
    } finally {
      setDownloading(null);
    }
  };

  const onActivate = async (row: PackVersionRow) => {
    const ok = await confirm({
      title: t("codebase.versions.confirmActivate", { number: row.number }),
      description: t("codebase.versions.confirmActivateDesc"),
      confirmLabel: t("codebase.versions.activate"),
    });
    if (ok) activate.mutate(row.number);
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <HistoryIcon aria-hidden className="size-4" /> {t("codebase.versions.title")}
        </CardTitle>
        <CardDescription>{t("codebase.versions.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <QueryState
          query={versions}
          empty={{
            icon: HistoryIcon,
            title: t("codebase.versions.emptyTitle"),
            description: t("codebase.versions.emptyDesc"),
          }}
        >
          {(data) => data.versions.length === 0 ? (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("codebase.versions.emptyDesc")}</p>
          ) : (
            <>
              <Table>
                <THead>
                  <TR>
                    <TH>{t("codebase.versions.version")}</TH>
                    <TH>{t("codebase.versions.uploaded")}</TH>
                    <TH>{t("codebase.versions.files")}</TH>
                    <TH>{t("codebase.versions.note")}</TH>
                    <TH><span className="sr-only">{t("codebase.versions.actions")}</span></TH>
                  </TR>
                </THead>
                <TBody>
                  {data.versions.map((row) => (
                    <TR key={row.number}>
                      <TD className="whitespace-nowrap">
                        <span className="font-medium">v{row.number}</span>{" "}
                        {row.current && <Badge variant="success">{t("codebase.versions.current")}</Badge>}
                        <div className="font-mono text-[11px] text-[var(--color-muted-foreground)]" title={row.sha}>
                          {row.sha.slice(0, 8)}
                        </div>
                      </TD>
                      <TD>
                        <div className="whitespace-nowrap">{formatDateTime(row.created_at)}</div>
                        {row.created_by && (
                          <div className="text-[11px] text-[var(--color-muted-foreground)]">{row.created_by}</div>
                        )}
                      </TD>
                      <TD className="whitespace-nowrap">
                        {t("codebase.versions.fileCount", { count: row.file_count })}
                        <div className="text-[11px] text-[var(--color-muted-foreground)]">
                          {formatBytes(row.total_bytes)}
                        </div>
                      </TD>
                      <TD className="max-w-xs break-words text-[var(--color-muted-foreground)]">{row.note}</TD>
                      <TD>
                        <div className="flex flex-wrap justify-end gap-1.5">
                          <Button
                            size="xs" variant="outline"
                            disabled={data.versions.length < 2}
                            onClick={() => setCompare(compare === row.number ? null : row.number)}
                          >
                            <GitCompareIcon />{t("codebase.versions.compare")}
                          </Button>
                          <Button
                            size="xs" variant="outline"
                            loading={downloading === row.number}
                            onClick={() => void download(row.number)}
                          >
                            <DownloadIcon />{t("codebase.versions.download")}
                          </Button>
                          {pack.can_maintain && !row.current && (
                            <Button
                              size="xs" variant="outline"
                              disabled={activate.isPending}
                              onClick={() => void onActivate(row)}
                            >
                              <Undo2Icon />{t("codebase.versions.activate")}
                            </Button>
                          )}
                        </div>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
              {compare !== null && (
                <DiffPanel
                  packId={pack.id}
                  number={compare}
                  numbers={data.versions.map((v) => v.number)}
                  onClose={() => setCompare(null)}
                />
              )}
            </>
          )}
        </QueryState>
      </CardContent>
      {dialog}
    </Card>
  );
}

/** Version `number` against another one (the previous retained version to
 *  begin with): files added, removed and changed. */
function DiffPanel({
  packId, number, numbers, onClose,
}: { packId: string; number: number; numbers: number[]; onClose: () => void }) {
  const t = useT();
  const token = useToken();
  const others = numbers.filter((n) => n !== number).sort((a, b) => b - a);
  const previous = others.find((n) => n < number) ?? others[0] ?? null;
  const [picked, setPicked] = useState<number | null>(null);
  const against = picked !== null && others.includes(picked) ? picked : previous;

  const diff = useQuery({
    queryKey: ["codebase", "diff", packId, number, against],
    queryFn: () => codebaseApi.diff(token!, packId, number, against!),
    enabled: !!token && against !== null,
  });

  const lists = diff.data ? [
    { key: "added", variant: "success", paths: diff.data.added },
    { key: "removed", variant: "destructive", paths: diff.data.removed },
    { key: "changed", variant: "warning", paths: diff.data.changed },
  ] as const : [];
  const total = lists.reduce((sum, l) => sum + l.paths.length, 0);

  return (
    <div className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">
          {t("codebase.diff.title", { number })}
        </span>
        <Select
          value={against !== null ? String(against) : ""}
          className="w-32"
          options={others.map((n) => ({ value: String(n), label: `v${n}` }))}
          onChange={(v) => setPicked(Number(v))}
        />
        <Button size="xs" variant="ghost" className="ml-auto" onClick={onClose}>
          {t("codebase.diff.close")}
        </Button>
      </div>
      {diff.isPending && against !== null && !diff.isError && <SkeletonRows rows={2} />}
      {diff.isError && <Callout tone="danger">{(diff.error as Error).message}</Callout>}
      {diff.data && total === 0 && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("codebase.diff.same")}</p>
      )}
      {lists.map((list) => list.paths.length > 0 && (
        <div key={list.key} className="space-y-1">
          <Badge variant={list.variant}>
            {t(`codebase.diff.${list.key}`, { count: list.paths.length })}
          </Badge>
          <ul className="max-h-48 space-y-0.5 overflow-y-auto font-mono text-[11px]">
            {list.paths.map((entry, i) => {
              const path = diffPath(entry);
              return <li key={`${path}:${i}`} className="break-all">{path}</li>;
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}
