"use client";

/**
 * One knowledge pack: what it holds, its versions, and — for the people who
 * maintain it — who it is shared with and how often agents used it.
 *
 * A reader and a maintainer open the same URL and get different tabs, not
 * different pages. What each may do is the API's call (`can_maintain` comes
 * from it); the tabs only stop offering what would be refused. A pack that
 * does not exist and a pack the caller may not see are the same 404 and the
 * same screen here.
 */

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeftIcon, BookMarkedIcon, DownloadIcon, Trash2Icon, UploadIcon } from "lucide-react";
import { toast } from "sonner";

import { ApiError, codebaseApi } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { AudienceBadge } from "@/components/codebase/audience";
import { ContentTab } from "@/components/codebase/content-tab";
import { ItemViewer } from "@/components/codebase/item-viewer";
import { SharingTab } from "@/components/codebase/sharing-tab";
import { UploadVersionDialog } from "@/components/codebase/upload-version";
import { UsageTab } from "@/components/codebase/usage-tab";
import { VersionsTab } from "@/components/codebase/versions-tab";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { QueryState } from "@/components/ui/query-state";
import { SegmentedControl } from "@/components/ui/segmented-control";

type Tab = "content" | "versions" | "sharing" | "usage";
const TABS: readonly Tab[] = ["content", "versions", "sharing", "usage"];

export default function PackPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const t = useT();
  const token = useToken();
  const router = useRouter();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [tab, setTab] = useState<Tab>("content");
  const [uploading, setUploading] = useState(false);
  const [openItem, setOpenItem] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  // Deep links: `?tab=sharing`, and `?item=<id>` from a search hit. Read from
  // the location rather than useSearchParams (which would need a Suspense
  // boundary) and applied a tick later, outside the effect body.
  useEffect(() => {
    const search = new URLSearchParams(window.location.search);
    const wantedTab = search.get("tab");
    const wantedItem = search.get("item");
    if (!wantedTab && !wantedItem) return;
    const timer = setTimeout(() => {
      if (wantedTab && (TABS as readonly string[]).includes(wantedTab)) setTab(wantedTab as Tab);
      if (wantedItem) setOpenItem(wantedItem);
    }, 0);
    return () => clearTimeout(timer);
  }, []);

  const pack = useQuery({
    queryKey: ["codebase", "pack", id],
    queryFn: () => codebaseApi.get(token!, id),
    enabled: !!token,
    retry: false,
  });
  const data = pack.data;

  const remove = useMutation({
    mutationFn: () => codebaseApi.remove(token!, id),
    onSuccess: () => {
      qc.removeQueries({ queryKey: ["codebase", "pack", id] });
      qc.invalidateQueries({ queryKey: ["codebase", "packs"] });
      toast.success(t("codebase.pack.deleted"));
      router.push("/codebase");
    },
    onError: (e) => toast.error(t("codebase.pack.deleteFailed", { message: (e as Error).message })),
  });

  const download = async () => {
    if (!data?.current_version) return;
    const number = data.current_version.number;
    setDownloading(true);
    try {
      await codebaseApi.downloadArchive(token!, id, number, `${data.slug}-v${number}.zip`);
    } catch (e) {
      toast.error(t("codebase.versions.downloadFailed", { message: (e as Error).message }));
    } finally {
      setDownloading(false);
    }
  };

  const onDelete = async () => {
    if (!data) return;
    const ok = await confirm({
      title: t("codebase.pack.confirmDelete", { title: data.title }),
      description: t("codebase.pack.confirmDeleteDesc"),
      confirmLabel: t("common.delete"),
      danger: true,
    });
    if (ok) remove.mutate();
  };

  const missing = pack.error instanceof ApiError && pack.error.status === 404;
  const maintainer = data?.can_maintain === true;
  const tabs: Tab[] = maintainer ? ["content", "versions", "sharing", "usage"] : ["content", "versions"];
  const active: Tab = tabs.includes(tab) ? tab : "content";

  return (
    <PageShell width="wide">
      <PageHeader
        tour="page-codebase-pack"
        icon={<BookMarkedIcon className="h-6 w-6" />}
        title={data?.title ?? t("codebase.pack.title")}
        badge={data && (
          <AudienceBadge audience={data.audience} teamCount={data.can_maintain ? data.teams.length : undefined} />
        )}
        description={data?.description || (data ? undefined : t("codebase.pack.subtitle"))}
        actions={data && (
          <>
            {data.current_version && (
              <Button variant="outline" loading={downloading} onClick={() => void download()}>
                <DownloadIcon />{t("codebase.pack.download")}
              </Button>
            )}
            {maintainer && (
              <Button onClick={() => setUploading(true)}>
                <UploadIcon />{t("codebase.upload.open")}
              </Button>
            )}
            {maintainer && (
              <Button variant="outline" loading={remove.isPending} onClick={() => void onDelete()}>
                <Trash2Icon />{t("common.delete")}
              </Button>
            )}
          </>
        )}
        tabs={<SectionTabs set="codebase" />}
      />

      {missing ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={BookMarkedIcon}
              title={t("codebase.pack.notFoundTitle")}
              description={t("codebase.pack.notFoundDesc")}
              action={
                <Link href="/codebase" className={buttonVariants({ variant: "outline" })}>
                  <ArrowLeftIcon />{t("codebase.pack.back")}
                </Link>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <QueryState query={pack}>
          {(p) => (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-[var(--color-muted-foreground)]">
                <code>{p.slug}</code>
                {p.current_version ? (
                  <Badge variant="outline">{t("codebase.card.version", { number: p.current_version.number })}</Badge>
                ) : (
                  <Badge variant="warning">{t("codebase.card.noVersion")}</Badge>
                )}
                <span>{t("codebase.card.files", { count: p.counts.files })}</span>
                <span>{t("codebase.card.skills", { count: p.counts.skills })}</span>
                <span>{t("codebase.card.updated", { date: formatDate(p.updated_at) })}</span>
                {p.created_by && <span>{t("codebase.pack.createdBy", { who: p.created_by })}</span>}
                {!maintainer && <span>{t("codebase.pack.readOnly")}</span>}
              </div>

              <SegmentedControl
                semantics="tabs"
                label={t("codebase.pack.sections")}
                value={active}
                onValueChange={setTab}
                segments={tabs.map((value) => ({
                  value,
                  label: t(`codebase.pack.tab.${value}`),
                  count: value === "content" ? p.items.length : undefined,
                }))}
              />

              {active === "content" && (
                <ContentTab
                  pack={p}
                  onOpen={setOpenItem}
                  onUpload={maintainer ? () => setUploading(true) : undefined}
                />
              )}
              {active === "versions" && <VersionsTab pack={p} />}
              {active === "sharing" && maintainer && <SharingTab pack={p} />}
              {active === "usage" && maintainer && <UsageTab pack={p} />}
            </>
          )}
        </QueryState>
      )}

      {maintainer && <UploadVersionDialog packId={id} open={uploading} onOpenChange={setUploading} />}
      <ItemViewer packId={id} itemId={openItem} onClose={() => setOpenItem(null)} />
      {dialog}
    </PageShell>
  );
}
