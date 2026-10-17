"use client";

/**
 * What a pack holds, grouped the way somebody looks for it: a card per skill
 * (its SKILL.md and the files that belong to it), then API examples,
 * checklists, notes and other documents.
 */

import { useState } from "react";
import { FileTextIcon, SparklesIcon, UploadIcon } from "lucide-react";

import type { PackDetail, PackItemRow } from "@/lib/api";
import { formatBytes, groupItems, KIND_ORDER } from "@/lib/codebase";
import { useT } from "@/lib/i18n";
import { CopyButton } from "@/components/codebase/copy";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";

/** Past this many files a filter box is worth its row. */
const FILTER_FROM = 8;

const KNOWN_KINDS: readonly string[] = KIND_ORDER;

function ItemRow({ item, onOpen }: { item: PackItemRow; onOpen: (id: string) => void }) {
  const t = useT();
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(item.id)}
        className="flex min-h-11 w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-[var(--color-accent)]/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)] sm:min-h-0"
      >
        <FileTextIcon aria-hidden className="size-4 shrink-0 text-[var(--color-muted-foreground)]" />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-xs">{item.path}</span>
          {item.description && (
            <span className="block truncate text-xs text-[var(--color-muted-foreground)]">
              {item.description}
            </span>
          )}
        </span>
        {item.kind === "skill" && <Badge variant="brand">{t("codebase.kind.skill")}</Badge>}
        <span className="shrink-0 text-xs text-[var(--color-muted-foreground)]">
          {formatBytes(item.size_bytes)}
        </span>
      </button>
    </li>
  );
}

export function ContentTab({
  pack,
  onOpen,
  onUpload,
}: {
  pack: PackDetail;
  onOpen: (itemId: string) => void;
  /** Present for maintainers: the empty state offers the first upload. */
  onUpload?: () => void;
}) {
  const t = useT();
  const [filter, setFilter] = useState("");

  if (!pack.current_version || pack.items.length === 0) {
    return (
      <Card>
        <CardContent>
          <EmptyState
            icon={UploadIcon}
            title={t("codebase.content.emptyTitle")}
            description={onUpload ? t("codebase.content.emptyMaintainer") : t("codebase.content.emptyReader")}
            action={onUpload && (
              <Button onClick={onUpload}><UploadIcon />{t("codebase.upload.open")}</Button>
            )}
          />
        </CardContent>
      </Card>
    );
  }

  const needle = filter.trim().toLowerCase();
  const items = needle
    ? pack.items.filter((i) => `${i.path} ${i.name} ${i.description}`.toLowerCase().includes(needle))
    : pack.items;
  const { skills, others } = groupItems(items);

  return (
    <div className="space-y-4">
      {pack.items.length > FILTER_FROM && (
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t("codebase.content.filter")}
          aria-label={t("codebase.content.filter")}
          className="sm:max-w-sm"
        />
      )}
      {skills.length === 0 && others.length === 0 && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("codebase.content.noMatch")}</p>
      )}
      {skills.map((group) => (
        <Card key={`skill:${group.skill}`}>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <CardTitle className="flex items-center gap-2 text-base">
                  <SparklesIcon aria-hidden className="size-4 text-[var(--color-brand)]" />
                  <span className="font-mono">{group.skill}</span>
                </CardTitle>
                {group.description && (
                  <CardDescription className="mt-1">{group.description}</CardDescription>
                )}
              </div>
              <CopyButton
                size="xs"
                label={t("codebase.content.copyCall")}
                text={`skills name="${pack.slug}/${group.skill}"`}
              />
            </div>
          </CardHeader>
          <CardContent className="p-0 sm:p-0">
            <ul className="divide-y divide-[var(--color-border)] overflow-hidden rounded-b-xl border-t border-[var(--color-border)]">
              {group.items.map((item) => <ItemRow key={item.id} item={item} onOpen={onOpen} />)}
            </ul>
          </CardContent>
        </Card>
      ))}
      {others.map((group) => (
        <Card key={`kind:${group.kind}`}>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              {KNOWN_KINDS.includes(group.kind) ? t(`codebase.kindPlural.${group.kind}`) : group.kind}
              <Badge variant="outline">{group.items.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0 sm:p-0">
            <ul className="divide-y divide-[var(--color-border)] overflow-hidden rounded-b-xl border-t border-[var(--color-border)]">
              {group.items.map((item) => <ItemRow key={item.id} item={item} onOpen={onOpen} />)}
            </ul>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
