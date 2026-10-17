"use client";

/**
 * Codebase: the knowledge packs this workspace has curated for its coding
 * agents — skills to follow, API examples, checklists, notes about how other
 * services do things.
 *
 * The list is whatever the API says this person may see. A pack that is not
 * shown here is indistinguishable from one that does not exist, so the page
 * never hints at hidden ones (no counts, no "N more").
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  BookMarkedIcon, CheckSquareIcon, CodeXmlIcon, PlugIcon, PlusIcon, SearchIcon, SparklesIcon,
  StickyNoteIcon,
} from "lucide-react";

import { codebaseApi, type PackOut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { PACK_AUTHOR_ROLES, roleLabel } from "@/lib/roles";
import { useToken } from "@/lib/use-token";
import { AudienceBadge } from "@/components/codebase/audience";
import { CreatePackDialog } from "@/components/codebase/create-pack";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";

export default function CodebasePage() {
  const t = useT();
  const token = useToken();
  const router = useRouter();
  const [creating, setCreating] = useState(false);
  const [q, setQ] = useState("");

  const packs = useQuery({
    queryKey: ["codebase", "packs"],
    queryFn: () => codebaseApi.list(token!),
    enabled: !!token,
  });
  const canCreate = packs.data?.can_create === true;

  return (
    <PageShell width="wide">
      <PageHeader
        tour="page-codebase"
        icon={<BookMarkedIcon className="h-6 w-6" />}
        title={t("codebase.title")}
        badge={<WorkspaceBadge />}
        description={t("codebase.subtitle")}
        actions={canCreate && (
          <Button data-tour="codebase-new-pack" onClick={() => setCreating(true)}>
            <PlusIcon />{t("codebase.newPack")}
          </Button>
        )}
        tabs={<SectionTabs set="codebase" />}
      />

      <QueryState query={packs}>
        {(data) => data.packs.length === 0 ? (
          <Card>
            <CardContent>
              <Empty canCreate={canCreate} onCreate={() => setCreating(true)} />
            </CardContent>
          </Card>
        ) : (
          <>
            <form
              role="search"
              className="flex max-w-xl gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                router.push(`/codebase/search${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`);
              }}
            >
              <Input
                value={q} maxLength={300}
                onChange={(e) => setQ(e.target.value)}
                placeholder={t("codebase.searchPlaceholder")}
                aria-label={t("codebase.searchPlaceholder")}
              />
              <Button type="submit" variant="outline"><SearchIcon />{t("codebase.searchButton")}</Button>
            </form>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {data.packs.map((pack) => <PackCard key={pack.id} pack={pack} />)}
            </div>
          </>
        )}
      </QueryState>

      <CreatePackDialog open={creating} onOpenChange={setCreating} />
    </PageShell>
  );
}

function PackCard({ pack }: { pack: PackOut }) {
  const t = useT();
  return (
    <Card className="relative flex flex-col transition-shadow hover:shadow-md">
      <CardHeader className="gap-1.5 pb-2">
        <CardTitle className="flex items-start justify-between gap-2 text-base">
          {/* The link stretches over the whole card, so the card is the target. */}
          <Link
            href={`/codebase/packs/${pack.id}`}
            className="min-w-0 break-words hover:underline after:absolute after:inset-0 after:rounded-xl focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-[var(--color-ring)]"
          >
            {pack.title}
          </Link>
          <AudienceBadge audience={pack.audience} teamCount={pack.can_maintain ? pack.teams.length : undefined} />
        </CardTitle>
        <code className="truncate text-xs text-[var(--color-muted-foreground)]">{pack.slug}</code>
        {pack.description && (
          <p className="line-clamp-2 text-xs leading-relaxed text-[var(--color-muted-foreground)]">
            {pack.description}
          </p>
        )}
      </CardHeader>
      <CardContent className="mt-auto space-y-3">
        <div className="flex flex-wrap gap-1.5">
          {pack.current_version ? (
            <Badge variant="outline">{t("codebase.card.version", { number: pack.current_version.number })}</Badge>
          ) : (
            <Badge variant="warning">{t("codebase.card.noVersion")}</Badge>
          )}
          {pack.counts.skills > 0 && (
            <Badge>{t("codebase.card.skills", { count: pack.counts.skills })}</Badge>
          )}
          {pack.counts.files > 0 && (
            <Badge>{t("codebase.card.files", { count: pack.counts.files })}</Badge>
          )}
          {pack.can_maintain && <Badge variant="brand">{t("codebase.card.maintainer")}</Badge>}
        </div>
        <div className="border-t border-[var(--color-border)] pt-2 text-xs text-[var(--color-muted-foreground)]">
          {t("codebase.card.updated", { date: formatDate(pack.updated_at) })}
        </div>
      </CardContent>
    </Card>
  );
}

const KINDS = [
  { key: "skill", icon: SparklesIcon },
  { key: "api", icon: CodeXmlIcon },
  { key: "checklist", icon: CheckSquareIcon },
  { key: "note", icon: StickyNoteIcon },
] as const;

/** Nothing visible yet. Says what a pack is, who can make one, and what to do
 *  next — which differs for somebody who can create packs and somebody who
 *  can only be shown them. */
function Empty({ canCreate, onCreate }: { canCreate: boolean; onCreate: () => void }) {
  const t = useT();
  return (
    <EmptyState
      icon={BookMarkedIcon}
      title={canCreate ? t("codebase.empty.title") : t("codebase.empty.titleReader")}
      description={canCreate ? t("codebase.empty.desc") : t("codebase.empty.descReader", {
        roles: PACK_AUTHOR_ROLES.map((r) => roleLabel(t, r)).join(", "),
      })}
      action={
        <>
          {canCreate && <Button onClick={onCreate}><PlusIcon />{t("codebase.newPack")}</Button>}
          <Link href="/codebase/connect" className={buttonVariants({ variant: "outline" })}>
            <PlugIcon />{t("codebase.empty.connect")}
          </Link>
        </>
      }
    >
      <ul className="mx-auto grid max-w-xl gap-2 text-left sm:grid-cols-2">
        {KINDS.map(({ key, icon: Icon }) => (
          <li key={key} className="flex items-start gap-2 rounded-lg border border-[var(--color-border)] p-3">
            <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-[var(--color-muted-foreground)]" />
            <span className="text-xs leading-relaxed">
              <span className="block font-medium">{t(`codebase.kind.${key}`)}</span>
              <span className="text-[var(--color-muted-foreground)]">{t(`codebase.kindDesc.${key}`)}</span>
            </span>
          </li>
        ))}
      </ul>
    </EmptyState>
  );
}
