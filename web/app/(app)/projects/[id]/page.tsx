"use client";

import { use, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeftIcon, PlusIcon, TrashIcon, MessagesSquareIcon, GitBranchIcon,
} from "lucide-react";
import { toast } from "sonner";

import {
  chatsApi, projectsApi,
  type ChatOut, type ProjectOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useQaRepos, type QaRepoOption } from "@/lib/use-qa-repos";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useT } from "@/lib/i18n";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

export default function ProjectDetailPage({
  params,
}: { params: Promise<{ id: string }> }) {
  // React 19 / Next.js 16 — async params, треба unwrap
  const { id } = use(params);
  const token = useToken();
  const qc = useQueryClient();
  const router = useRouter();
  const t = useT();
  const { confirm, dialog } = useConfirm();

  const project = useQuery({
    queryKey: ["projects", id],
    queryFn: () => projectsApi.get(token!, id),
    enabled: !!token,
  });
  const chats = useQuery({
    queryKey: ["chats", { project_id: id }],
    queryFn: () => chatsApi.list(token!, { project_id: id }),
    enabled: !!token,
  });
  // All workspace repos + their vault readiness (not just vault-ready ones).
  const repos = useQaRepos();

  const newChatMut = useMutation({
    mutationFn: () => chatsApi.create(token!, { project_id: id, name: null }),
    onSuccess: (chat) => {
      router.push(`/projects/${id}/chats/${chat.id}`);
    },
    onError: (e) =>
      toast.error(t("projects.detail.failed", { error: e instanceof Error ? e.message : String(e) })),
  });

  const addRepoMut = useMutation({
    mutationFn: ({ repo_slug, role }: { repo_slug: string; role?: string }) =>
      projectsApi.addRepo(token!, id, { repo_slug, role: role || null }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects", id] });
      toast.success(t("projects.detail.repoAdded"));
    },
    onError: (e) =>
      toast.error(t("projects.detail.failed", { error: e instanceof Error ? e.message : String(e) })),
  });

  const removeRepoMut = useMutation({
    mutationFn: (slug: string) => projectsApi.removeRepo(token!, id, slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects", id] });
      toast.success(t("projects.detail.repoRemoved"));
    },
    onError: (e) =>
      toast.error(t("projects.detail.failed", { error: e instanceof Error ? e.message : String(e) })),
  });

  const deleteChatMut = useMutation({
    mutationFn: (chatId: string) => chatsApi.delete(token!, chatId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chats", { project_id: id }] });
      toast.success(t("projects.detail.chatDeleted"));
    },
    onError: (e) =>
      toast.error(t("projects.detail.failed", { error: e instanceof Error ? e.message : String(e) })),
  });

  if (project.isLoading) {
    return <div className="p-8 text-center text-muted-foreground">{t("projects.detail.loading")}</div>;
  }
  if (project.error) {
    return <div className="p-8 text-destructive">{(project.error as Error).message}</div>;
  }
  if (!project.data) return null;

  const p = project.data;
  const usedRepoSlugs = new Set(p.repos.map((r) => r.repo_slug));
  const availableForAdd = repos.options.filter((r) => !usedRepoSlugs.has(r.slug));
  // Undefined while the repo lists load — only an explicit `false` flags "no vault".
  const vaultBySlug = new Map(repos.options.map((r) => [r.slug, r.hasVault]));

  return (
    <PageShell width="wide">
      <div>
        <Link
          href="/projects"
          className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1 mb-2"
        >
          <ArrowLeftIcon className="h-3 w-3" />
          {t("projects.detail.backToProjects")}
        </Link>
        <h1 className="text-2xl font-semibold">{p.name}</h1>
        {p.description && (
          <p className="text-sm text-muted-foreground mt-1">{p.description}</p>
        )}
      </div>

      <SectionTabs set="qa" />

      <div className="grid gap-6 lg:grid-cols-3">
        {/* ─── Left col — repos ────────────────────────────────────── */}
        <div className="lg:col-span-1">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-base">
                <span className="flex items-center gap-2">
                  <GitBranchIcon className="h-4 w-4" />
                  {t("projects.detail.reposCount", { count: p.repos.length })}
                </span>
                <AddRepoDialog
                  availableRepos={availableForAdd}
                  onAdd={(repo_slug, role) => addRepoMut.mutate({ repo_slug, role })}
                />
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {p.repos.length === 0 && (
                <p className="text-xs text-muted-foreground py-4 text-center">
                  {t("projects.detail.noRepos")}
                </p>
              )}
              {p.repos.map((r) => (
                <div
                  key={r.repo_slug}
                  className="flex items-center justify-between rounded border px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{r.repo_slug}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {r.role && (
                        <Badge variant="outline" className="text-[10px]">
                          {r.role}
                        </Badge>
                      )}
                      {vaultBySlug.get(r.repo_slug) === false && (
                        <Badge variant="outline" className="text-[10px]">
                          {t("projects.repoNoVault")}
                        </Badge>
                      )}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={async () => {
                      const ok = await confirm({
                        title: t("projects.detail.removeRepoConfirm", { slug: r.repo_slug }),
                        confirmLabel: t("common.remove"),
                        danger: true,
                      });
                      if (ok) removeRepoMut.mutate(r.repo_slug);
                    }}
                  >
                    <TrashIcon className="h-3 w-3" />
                  </Button>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        {/* ─── Right col — chats ───────────────────────────────────── */}
        <div className="lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-base">
                <span className="flex items-center gap-2">
                  <MessagesSquareIcon className="h-4 w-4" />
                  {t("projects.detail.chatsCount", { count: chats.data?.length ?? 0 })}
                </span>
                <Button
                  size="sm"
                  onClick={() => newChatMut.mutate()}
                  disabled={p.repos.length === 0 || newChatMut.isPending}
                >
                  <PlusIcon className="h-4 w-4 mr-1" />
                  {t("projects.detail.newChat")}
                </Button>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {chats.data && chats.data.length === 0 && (
                <p className="text-xs text-muted-foreground py-6 text-center">
                  {t("projects.detail.noChats", { count: p.repos.length })}
                </p>
              )}
              {chats.data?.map((c) => (
                <ChatRow
                  key={c.id}
                  chat={c}
                  projectId={id}
                  onDelete={async () => {
                    const ok = await confirm({
                      title: t("projects.detail.deleteChatConfirm"),
                      confirmLabel: t("common.delete"),
                      danger: true,
                    });
                    if (ok) deleteChatMut.mutate(c.id);
                  }}
                />
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
      {dialog}
    </PageShell>
  );
}

function ChatRow({
  chat, projectId, onDelete,
}: { chat: ChatOut; projectId: string; onDelete: () => void }) {
  const t = useT();
  return (
    <div className="flex items-center justify-between rounded border px-3 py-2 hover:bg-accent">
      <Link
        href={`/projects/${projectId}/chats/${chat.id}`}
        className="flex-1 text-sm"
      >
        <div className="font-medium truncate">
          {chat.name || t("projects.detail.chatNameFallback", { id: chat.id.slice(0, 8) })}
        </div>
        <div className="text-xs text-muted-foreground">
          {t("projects.detail.chatMeta", {
            count: chat.messages_count,
            date: new Date(chat.updated_at).toLocaleString(),
          })}
        </div>
      </Link>
      <Button
        variant="ghost"
        size="sm"
        onClick={(e) => {
          e.preventDefault();
          onDelete();
        }}
      >
        <TrashIcon className="h-3 w-3" />
      </Button>
    </div>
  );
}

function AddRepoDialog({
  availableRepos, onAdd,
}: { availableRepos: QaRepoOption[]; onAdd: (slug: string, role?: string) => void }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [slug, setSlug] = useState("");
  const [role, setRole] = useState("");

  // Pick from the real workspace list instead of typing a slug blind.
  const options = availableRepos.map((r) => ({
    value: r.slug,
    label: r.hasVault
      ? `${r.slug} · ${t("projects.detail.vaultPoints", { points: r.vaultPoints })}`
      : `${r.slug} · ${t("projects.repoNoVault")}`,
  }));
  const picked = availableRepos.find((r) => r.slug === slug);

  const handleAdd = () => {
    if (!slug) return;
    onAdd(slug, role.trim() || undefined);
    setSlug("");
    setRole("");
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <PlusIcon className="h-3 w-3 mr-1" />
          {t("projects.detail.add")}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("projects.detail.addRepoTitle")}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="add-repo-slug">{t("projects.detail.repoSlugLabel")}</Label>
            {options.length === 0 ? (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("projects.detail.noReposToAdd")}{" "}
                <Link href="/repositories" className="underline">
                  {t("projects.noReposCta")}
                </Link>
              </p>
            ) : (
              <Select
                id="add-repo-slug"
                className="w-full"
                value={slug}
                onChange={setSlug}
                options={options}
                placeholder={t("projects.detail.selectRepoPlaceholder")}
              />
            )}
            {picked && !picked.hasVault && (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("projects.repoNoVaultHint")}{" "}
                <Link href="/repositories" className="underline">
                  {t("projects.noReposCta")}
                </Link>
              </p>
            )}
          </div>
          <div className="space-y-1">
            <Label htmlFor="add-repo-role">{t("projects.detail.roleLabel")}</Label>
            <Input
              id="add-repo-role"
              placeholder="frontend / backend / shared"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={handleAdd} disabled={!slug}>{t("projects.detail.add")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
