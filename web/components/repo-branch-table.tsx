"use client";

/**
 * Which branch each repository is audited at, editable in place.
 *
 * The run-wide override answers "check develop everywhere, once". This answers
 * the other half: repositories that permanently live on different branches —
 * one on `main`, one on `dev`, one still on `master` — which until now could
 * only be fixed by visiting Repositories and editing them one at a time, three
 * pages away from the audit that depends on the value.
 *
 * Saving writes the branch onto the registration (PATCH /api/repos/{slug}/branch),
 * so it persists for indexing and review too. A run-wide override wins for the
 * next run and is deliberately NOT saved here — the two are different questions
 * and merging them is how a temporary check becomes a permanent scope nobody
 * remembers setting.
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { GitBranchIcon, Loader2Icon } from "lucide-react";

import { api, branchesApi, type RepoOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { BranchCombobox, toBranchResult } from "@/components/branch-combobox";

/** Sentinel for "no branch saved" — an empty string is a valid picker value
 *  but not a valid branch, which is exactly the distinction needed. */
const PROVIDER_DEFAULT = "";

export function RepoBranchTable({
  repos,
  overrideBranch,
}: {
  repos: RepoOut[];
  /** Run-wide override, shown as what will actually be read. Empty = none. */
  overrideBranch: string;
}) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [edits, setEdits] = useState<Record<string, string>>({});

  // No branch list is fetched until a row's picker is opened: it is a
  // provider call per repository, and most rows are never touched.

  const dirty = useMemo(
    () => repos.filter((r) => r.slug in edits && edits[r.slug] !== (r.branch ?? "")),
    [repos, edits],
  );

  const save = useMutation({
    mutationFn: async () => {
      // Sequential rather than parallel: each one writes the same registry and
      // a failure halfway should leave a knowable state, not a random subset.
      for (const r of dirty) {
        await api<RepoOut>(`/api/repos/${r.slug}/branch`, {
          token, method: "PATCH",
          json: { branch: edits[r.slug] || null },
        });
      }
    },
    onSuccess: () => {
      toast.success(t("deps.branchTableSaved", { count: dirty.length }));
      setEdits({});
      void qc.invalidateQueries({ queryKey: ["repos"] });
      void qc.invalidateQueries({ queryKey: ["deps-repos"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  if (repos.length === 0) return null;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-medium">{t("deps.branchTableTitle")}</span>
        <Button
          size="sm"
          variant={dirty.length ? "default" : "outline"}
          disabled={dirty.length === 0 || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending && <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />}
          {dirty.length
            ? t("deps.branchTableSave", { count: dirty.length })
            : t("deps.branchTableNoChanges")}
        </Button>
      </div>

      <div className="max-h-72 space-y-1.5 overflow-y-auto">
        {repos.map((r) => {
          const saved = r.branch ?? PROVIDER_DEFAULT;
          const current = edits[r.slug] ?? saved;
          return (
            <div
              key={r.slug}
              className="grid grid-cols-1 items-center gap-1.5 sm:grid-cols-[1fr_14rem]"
            >
              <span className="min-w-0 truncate text-xs text-[var(--color-muted-foreground)]">
                {r.full_name}
              </span>
              <div className="flex items-center gap-1.5">
                {/* A branch saved on the registration but absent from the
                    provider list (renamed, deleted, or the list failed to
                    load) stays the shown value — the picker never rewrites
                    the setting just by being opened. */}
                <BranchCombobox
                  value={current}
                  onChange={(v) => setEdits((prev) => ({ ...prev, [r.slug]: v }))}
                  search={(q) => branchesApi.forRepo(token!, r.slug, q).then(toBranchResult)}
                  queryKey={["repo-branches", r.slug]}
                  leadingOptions={[
                    { value: PROVIDER_DEFAULT, label: t("deps.branchProviderDefault") },
                  ]}
                  disabled={!token}
                  ariaLabel={r.full_name}
                  className="w-full min-w-0"
                  placeholder={t("deps.branchPickPlaceholder")}
                />
                {/* What this run will ACTUALLY read, which is not the saved
                    value while an override is set. Saying so here is the
                    whole point of the table. */}
                {overrideBranch && (
                  <span
                    className="shrink-0 text-[10px] text-[var(--color-brand)]"
                    title={t("deps.branchTableOverridden")}
                  >
                    <GitBranchIcon className="inline h-3 w-3" /> {overrideBranch}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {overrideBranch && (
        <p className="text-xs text-[var(--color-brand)]">
          {t("deps.branchTableOverrideNote", { branch: overrideBranch })}
        </p>
      )}
    </div>
  );
}
