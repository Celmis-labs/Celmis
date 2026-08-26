"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { FolderTreeIcon, GitBranchIcon, PlusIcon, TrashIcon, MessagesSquareIcon } from "lucide-react";
import { toast } from "sonner";

import { projectsApi, type ProjectOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useQaRepos, type QaRepoOption } from "@/lib/use-qa-repos";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip } from "@/components/ui/tooltip";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { WorkspaceBadge } from "@/components/workspace-badge";

export default function ProjectsPage() {
  const token = useToken();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const t = useT();

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => projectsApi.list(token!),
    enabled: !!token,
  });

  // Every workspace repo (not only the vault-ready ones — see useQaRepos).
  const repos = useQaRepos();

  const deleteMut = useMutation({
    mutationFn: (id: string) => projectsApi.delete(token!, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.success(t("projects.toastDeleted"));
    },
    onError: (e) =>
      toast.error(t("projects.toastDeleteFailed", { error: e instanceof Error ? e.message : String(e) })),
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<FolderTreeIcon className="h-6 w-6" />}
        title={t("projects.title")}
        badge={<WorkspaceBadge />}
        description={t("projects.subtitle")}
        actions={
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button>
                <PlusIcon className="h-4 w-4 mr-1" />
                {t("projects.newProject")}
              </Button>
            </DialogTrigger>
            <CreateProjectDialog
              repos={repos.options}
              onCreated={() => {
                setOpen(false);
                qc.invalidateQueries({ queryKey: ["projects"] });
              }}
            />
          </Dialog>
        }
        tabs={<SectionTabs set="qa" />}
      />

      {projects.isLoading && (
        <Card><CardContent className="py-8 text-center text-muted-foreground">{t("projects.loading")}</CardContent></Card>
      )}
      {projects.error && (
        <Card><CardContent className="py-8 text-destructive">{(projects.error as Error).message}</CardContent></Card>
      )}
      {projects.data && projects.data.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center space-y-3">
            <p className="text-muted-foreground">{t("projects.noProjects")}</p>
            <p className="text-xs text-muted-foreground">
              {t("projects.noProjectsHint")}
            </p>
          </CardContent>
        </Card>
      )}

      {projects.data && projects.data.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {projects.data.map((p) => (
            <ProjectCard
              key={p.id}
              project={p}
              onDelete={() => deleteMut.mutate(p.id)}
            />
          ))}
        </div>
      )}

      {!repos.isLoading && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">{t("projects.availableReposTitle")}</CardTitle>
          </CardHeader>
          <CardContent>
            {repos.options.length === 0 ? (
              <EmptyState
                icon={GitBranchIcon}
                title={t("projects.noReposTitle")}
                description={t("projects.noReposDescription")}
                action={
                  <Link href="/repositories" className={buttonVariants({ size: "sm" })}>
                    {t("projects.noReposCta")}
                  </Link>
                }
              />
            ) : (
              <div className="flex flex-wrap gap-2">
                {repos.options.map((r) => (
                  <RepoBadge key={r.slug} repo={r} />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </PageShell>
  );
}

/** Repo chip: vault-ready ones carry their point count in a tooltip. */
function RepoBadge({ repo }: { repo: QaRepoOption }) {
  const t = useT();
  if (!repo.hasVault) {
    return (
      <Tooltip label={t("projects.repoNoVaultHint")}>
        <Badge variant="outline" className="gap-1.5">
          {repo.slug}
          <span className="text-[10px] text-[var(--color-muted-foreground)]">
            {t("projects.repoNoVault")}
          </span>
        </Badge>
      </Tooltip>
    );
  }
  return (
    <Tooltip label={t("projects.pointsCount", { count: repo.vaultPoints })}>
      <Badge variant="default">{repo.slug}</Badge>
    </Tooltip>
  );
}

function ProjectCard({ project, onDelete }: { project: ProjectOut; onDelete: () => void }) {
  const t = useT();
  const { confirm, dialog } = useConfirm();
  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-base">
          <Link href={`/projects/${project.id}`} className="hover:underline">
            {project.name}
          </Link>
          <Button
            variant="ghost"
            size="sm"
            onClick={async (e) => {
              e.preventDefault();
              const ok = await confirm({
                title: t("projects.confirmDelete", { name: project.name }),
                confirmLabel: t("common.delete"),
                danger: true,
              });
              if (ok) onDelete();
            }}
          >
            <TrashIcon className="h-4 w-4 text-muted-foreground" />
          </Button>
        </CardTitle>
        {project.description && (
          <p className="text-xs text-muted-foreground">{project.description}</p>
        )}
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex flex-wrap gap-1">
          {project.repos.map((r) => (
            <Badge key={r.repo_slug} variant="outline" className="text-xs">
              {r.repo_slug}{r.role ? ` · ${r.role}` : ""}
            </Badge>
          ))}
        </div>
        <div className="flex items-center justify-between text-xs text-muted-foreground pt-2 border-t">
          <span>{t("projects.repoCount", { count: project.repos.length })}</span>
          <Link href={`/projects/${project.id}`} className="flex items-center gap-1 hover:underline">
            <MessagesSquareIcon className="h-3 w-3" /> {t("projects.chatsCount", { count: project.chats_count })}
          </Link>
        </div>
      </CardContent>
      {dialog}
    </Card>
  );
}

function CreateProjectDialog({
  repos, onCreated,
}: { repos: QaRepoOption[]; onCreated: () => void }) {
  const token = useToken();
  const t = useT();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(new Set());
  const [roles, setRoles] = useState<Record<string, string>>({});

  const createMut = useMutation({
    mutationFn: () => projectsApi.create(token!, {
      name: name.trim(),
      description: description.trim() || null,
      repos: Array.from(selectedRepos).map((s) => ({
        repo_slug: s, role: roles[s] || null,
      })),
    }),
    onSuccess: () => {
      toast.success(t("projects.toastCreated"));
      onCreated();
    },
    onError: (e) =>
      toast.error(t("projects.toastCreateFailed", { error: e instanceof Error ? e.message : String(e) })),
  });

  const toggle = (slug: string) => {
    const next = new Set(selectedRepos);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    setSelectedRepos(next);
  };

  // Picking a vault-less repo is allowed (the vault can be generated later),
  // but the answers stay non-semantic until it exists — say so.
  const selectedWithoutVault = repos
    .filter((r) => selectedRepos.has(r.slug) && !r.hasVault)
    .map((r) => r.slug);

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{t("projects.createTitle")}</DialogTitle>
        <DialogDescription>
          {t("projects.createDescription")}
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-3">
        <div className="space-y-1">
          <Label htmlFor="proj-name">{t("projects.nameLabel")}</Label>
          <Input
            id="proj-name"
            placeholder={t("projects.namePlaceholder")}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="proj-desc">{t("projects.descriptionLabel")}</Label>
          <Textarea
            id="proj-desc"
            placeholder={t("projects.descriptionPlaceholder")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
          />
        </div>

        <div className="space-y-1">
          <Label>{t("projects.selectReposLabel")}</Label>
          {repos.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {t("projects.noReposDescription")}{" "}
              <Link href="/repositories" className="underline">
                {t("projects.noReposCta")}
              </Link>
            </p>
          ) : (
            <div className="max-h-60 overflow-y-auto space-y-1 border rounded p-2">
              {repos.map((r) => (
                <label
                  key={r.slug}
                  className="flex items-center justify-between gap-2 px-2 py-1 rounded hover:bg-accent cursor-pointer"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <input
                      type="checkbox"
                      checked={selectedRepos.has(r.slug)}
                      onChange={() => toggle(r.slug)}
                    />
                    <span className="truncate text-sm">{r.slug}</span>
                    {r.hasVault ? (
                      <Tooltip label={t("projects.pointsCount", { count: r.vaultPoints })}>
                        <Badge variant="outline" className="text-[10px]">
                          {t("projects.repoVaultReady")}
                        </Badge>
                      </Tooltip>
                    ) : (
                      <Tooltip label={t("projects.repoNoVaultHint")}>
                        <Badge variant="outline" className="text-[10px]">
                          {t("projects.repoNoVault")}
                        </Badge>
                      </Tooltip>
                    )}
                  </div>
                  {selectedRepos.has(r.slug) && (
                    <Input
                      placeholder={t("projects.rolePlaceholder")}
                      className="h-11 sm:h-6 w-32 shrink-0 text-base sm:text-xs"
                      value={roles[r.slug] ?? ""}
                      onChange={(e) => setRoles((prev) => ({ ...prev, [r.slug]: e.target.value }))}
                    />
                  )}
                </label>
              ))}
            </div>
          )}
          {selectedWithoutVault.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {t("projects.selectedNoVaultHint", { repos: selectedWithoutVault.join(", ") })}{" "}
              <Link href="/repositories" className="underline">
                {t("projects.noReposCta")}
              </Link>
            </p>
          )}
        </div>
      </div>

      <DialogFooter>
        <Button
          onClick={() => createMut.mutate()}
          disabled={!name.trim() || createMut.isPending}
        >
          {createMut.isPending ? t("projects.creating") : t("projects.create")}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
