"use client";

/** Include / exclude globs of one repository inside a project (one pattern per line). */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { projectsApi, type ProjectRepoOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

const lines = (text: string): string[] =>
  text.split("\n").map((l) => l.trim()).filter(Boolean);

export function ProjectFileScope({
  projectId, repo,
}: { projectId: string; repo: ProjectRepoOut }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [include, setInclude] = useState((repo.include_globs ?? []).join("\n"));
  const [exclude, setExclude] = useState((repo.exclude_globs ?? []).join("\n"));
  const save = useMutation({
    mutationFn: () =>
      projectsApi.setScope(token!, projectId, repo.repo_slug, {
        include_globs: lines(include),
        exclude_globs: lines(exclude),
      }),
    onSuccess: () => {
      toast.success(t("projects.scope.saved"));
      void qc.invalidateQueries({ queryKey: ["projects", projectId] });
      setOpen(false);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const active = (repo.include_globs?.length ?? 0) + (repo.exclude_globs?.length ?? 0);

  if (!open) {
    return (
      <Button variant="ghost" size="sm" className="h-6 px-1 text-xs"
        onClick={() => setOpen(true)}>
        {t("projects.scope.edit")}{active > 0 ? ` (${active})` : ""}
      </Button>
    );
  }
  return (
    <div className="mt-2 space-y-2">
      <p className="text-xs text-muted-foreground">{t("projects.scope.hint")}</p>
      <div>
        <Label htmlFor={`inc-${repo.repo_slug}`}>{t("projects.scope.include")}</Label>
        <textarea id={`inc-${repo.repo_slug}`} value={include} rows={3}
          onChange={(e) => setInclude(e.target.value)}
          placeholder="src/**"
          className="w-full rounded border bg-transparent px-2 py-1 font-mono text-xs" />
      </div>
      <div>
        <Label htmlFor={`exc-${repo.repo_slug}`}>{t("projects.scope.exclude")}</Label>
        <textarea id={`exc-${repo.repo_slug}`} value={exclude} rows={3}
          onChange={(e) => setExclude(e.target.value)}
          placeholder="**/generated/**"
          className="w-full rounded border bg-transparent px-2 py-1 font-mono text-xs" />
      </div>
      <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending || !token}>
        {t("projects.scope.save")}
      </Button>
    </div>
  );
}
