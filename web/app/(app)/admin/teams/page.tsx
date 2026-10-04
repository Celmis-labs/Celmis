"use client";

/**
 * /admin/teams — RBAC management.
 *
 * A team groups users. Repos are granted to teams with a permission level
 * (read | review | admin). A user's effective permission on a repo is the
 * highest across every team they're in. `is_admin=True` on the user always
 * wins (see /api/teams/me).
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  PlusIcon, Trash2Icon, UsersIcon,
} from "lucide-react";

import { api, teamsApi, type RepoOut, type Team } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { TEAM_ROLES } from "@/lib/roles";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

/** How long typing pauses before the member list is asked again. */
const MEMBER_SEARCH_DEBOUNCE_MS = 250;

// One list for the API and the UI: TEAM_ROLES in src/users/roles.py.
const MEMBER_ROLE_OPTIONS = TEAM_ROLES.map((r) => ({ value: r, label: r }));
const REPO_PERM_OPTIONS = ["admin", "review", "read"].map((r) => ({ value: r, label: r }));

export default function TeamsPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  const list = useQuery({
    queryKey: ["teams"],
    queryFn: () => teamsApi.list(token!),
    enabled: !!token,
  });

  const create = useMutation({
    mutationFn: () => teamsApi.create(token!, newName.trim(), newDesc.trim()),
    onSuccess: () => {
      setNewName("");
      setNewDesc("");
      qc.invalidateQueries({ queryKey: ["teams"] });
      toast.success(t("admin.teams.teamCreated"));
    },
    onError: (e) => toast.error(t("admin.teams.createFailed", { message: (e as Error).message })),
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<UsersIcon className="h-6 w-6" />}
        title={t("admin.teams.title")}
        description={
          <>
            {t("admin.teams.description")} <code>is_admin</code>{" "}
            {t("admin.teams.descriptionSuffix")}
          </>
        }
        tabs={<SectionTabs set="team" />}
      />

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.teams.createTeamTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] items-end">
          <div>
            <Label>{t("admin.teams.nameLabel")}</Label>
            <Input value={newName} onChange={(e) => setNewName(e.target.value)}
                   placeholder={t("admin.teams.namePlaceholder")} />
          </div>
          <div>
            <Label>{t("admin.teams.descriptionLabel")}</Label>
            <Input value={newDesc} onChange={(e) => setNewDesc(e.target.value)}
                   placeholder={t("admin.teams.descriptionPlaceholder")} />
          </div>
          <Button onClick={() => create.mutate()}
                  disabled={create.isPending || !newName.trim()}>
            <PlusIcon className="h-4 w-4 mr-1" />
            {create.isPending ? "…" : t("admin.teams.add")}
          </Button>
        </CardContent>
      </Card>

      {list.isLoading && (
        <div className="text-sm text-[var(--color-muted-foreground)]">{t("admin.teams.loading")}</div>
      )}

      {(list.data ?? []).map((team) => (
        <TeamCard key={team.id} team={team} />
      ))}
    </PageShell>
  );
}


function TeamCard({ team }: { team: Team }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [expanded, setExpanded] = useState(false);
  const [newUserId, setNewUserId] = useState("");
  const [newUserRole, setNewUserRole] = useState("reviewer");
  const [newRepoSlug, setNewRepoSlug] = useState("");
  const [newRepoPerm, setNewRepoPerm] = useState("review");

  const members = useQuery({
    queryKey: ["teams", team.id, "members"],
    queryFn: () => teamsApi.members(token!, team.id),
    enabled: !!token && expanded,
  });
  const repos = useQuery({
    queryKey: ["teams", team.id, "repos"],
    queryFn: () => teamsApi.repos(token!, team.id),
    enabled: !!token && expanded,
  });
  // A GRANT IS ADDRESSED, NOT DESCRIBED. This was a free-text box whose
  // placeholder read "owner/repo", while the sibling Access page next door
  // binds the same kind of grant to r.slug — the indexed slug that every
  // {repo_slug} route, the filesystem and five tables are keyed on. So which
  // enforcement saw a grant depended on which admin page it was typed into.
  // The list is the same one the Access page uses.
  const workspaceRepos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token && expanded,
  });

  const remove = useMutation({
    mutationFn: () => teamsApi.remove(token!, team.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["teams"] });
      toast.success(t("admin.teams.teamRemoved"));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const addMember = useMutation({
    mutationFn: () => teamsApi.upsertMember(token!, team.id, newUserId.trim(), newUserRole),
    onSuccess: () => {
      setNewUserId("");
      qc.invalidateQueries({ queryKey: ["teams", team.id, "members"] });
      qc.invalidateQueries({ queryKey: ["teams", team.id, "candidates"] });
      qc.invalidateQueries({ queryKey: ["teams"] });
    },
    onError: (e) => toast.error(t("admin.teams.addFailed", { message: (e as Error).message })),
  });

  const removeMember = useMutation({
    mutationFn: (uid: string) => teamsApi.removeMember(token!, team.id, uid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["teams", team.id, "members"] });
      qc.invalidateQueries({ queryKey: ["teams", team.id, "candidates"] });
      qc.invalidateQueries({ queryKey: ["teams"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const grantRepo = useMutation({
    mutationFn: () => teamsApi.grantRepo(token!, team.id, newRepoSlug.trim(), newRepoPerm),
    onSuccess: () => {
      setNewRepoSlug("");
      qc.invalidateQueries({ queryKey: ["teams", team.id, "repos"] });
    },
    onError: (e) => toast.error(t("admin.teams.grantFailed", { message: (e as Error).message })),
  });

  const revokeRepo = useMutation({
    mutationFn: (slug: string) => teamsApi.revokeRepo(token!, team.id, slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["teams", team.id, "repos"] }),
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader
        className="cursor-pointer"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="flex justify-between items-center gap-3">
          <div>
            <CardTitle>{team.name}</CardTitle>
            {team.description && (
              <CardDescription className="mt-1">{team.description}</CardDescription>
            )}
          </div>
          <div className="flex gap-2 items-center">
            <Badge variant="outline">{t("admin.teams.membersCount", { count: team.member_count })}</Badge>
            <Button
              variant="ghost" size="icon"
              onClick={async (e) => {
                e.stopPropagation();
                const ok = await confirm({
                  title: t("admin.teams.deleteConfirm", { name: team.name }),
                  confirmLabel: t("common.delete"),
                  danger: true,
                });
                if (ok) remove.mutate();
              }}
              aria-label={t("admin.teams.deleteAriaLabel")}
            >
              <Trash2Icon className="h-4 w-4 text-red-600" />
            </Button>
          </div>
        </div>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-6">
          {/* Members */}
          <div>
            <h3 className="text-sm font-medium mb-2">{t("admin.teams.membersHeading")}</h3>
            <div className="space-y-1">
              {(members.data ?? []).map((m) => (
                <div key={m.user_id} className="flex justify-between items-center gap-2 text-sm">
                  <span className="min-w-0 truncate" title={m.user_id}>
                    {m.name || m.email || <code>{m.user_id}</code>}
                    {m.name && m.email && (
                      <span className="ml-1.5 text-xs text-[var(--color-muted-foreground)]">
                        {m.email}
                      </span>
                    )}
                    {" · "}<Badge variant="outline">{m.role}</Badge>
                  </span>
                  <Button
                    variant="ghost" size="icon"
                    onClick={async () => {
                      const ok = await confirm({
                        title: t("admin.teams.removeMemberConfirm"),
                        description: m.name || m.email || m.user_id,
                        confirmLabel: t("common.remove"),
                        danger: true,
                      });
                      if (ok) removeMember.mutate(m.user_id);
                    }}
                  >
                    <Trash2Icon className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              {members.data?.length === 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("admin.teams.noMembers")}
                </p>
              )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto_auto] gap-2 mt-2 items-start">
              <MemberPicker teamId={team.id} value={newUserId} onChange={setNewUserId} />
              <Select
                className="h-11 sm:h-8"
                value={newUserRole} onChange={(v) => setNewUserRole(v)}
                options={MEMBER_ROLE_OPTIONS}
              />
              <Button onClick={() => addMember.mutate()} disabled={!newUserId.trim() || addMember.isPending}>
                {t("admin.teams.add")}
              </Button>
            </div>
          </div>

          {/* Repo grants */}
          <div>
            <h3 className="text-sm font-medium mb-2">{t("admin.teams.repoAccessHeading")}</h3>
            <div className="space-y-1">
              {(repos.data ?? []).map((r) => (
                <div key={r.repo_slug} className="flex justify-between items-center text-sm">
                  <span><code>{r.repo_slug}</code> · <Badge variant="outline">{r.permission}</Badge></span>
                  <Button
                    variant="ghost" size="icon"
                    onClick={async () => {
                      const ok = await confirm({
                        title: t("admin.teams.revokeRepoConfirm", { slug: r.repo_slug }),
                        confirmLabel: t("common.remove"),
                        danger: true,
                      });
                      if (ok) revokeRepo.mutate(r.repo_slug);
                    }}
                  >
                    <Trash2Icon className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              {repos.data?.length === 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("admin.teams.noRepos")}
                </p>
              )}
            </div>
            <div className="grid grid-cols-[1fr_auto_auto] gap-2 mt-2 items-end">
              <Select
                className="h-11 sm:h-8"
                value={newRepoSlug}
                onChange={(v) => setNewRepoSlug(v)}
                disabled={workspaceRepos.isLoading}
                options={(workspaceRepos.data ?? []).map((r) => ({
                  value: r.slug,
                  label: r.full_name || r.slug,
                }))}
                placeholder={t("admin.teams.repoSlugPlaceholder")}
              />
              <Select
                className="h-11 sm:h-8"
                value={newRepoPerm} onChange={(v) => setNewRepoPerm(v)}
                options={REPO_PERM_OPTIONS}
              />
              <Button onClick={() => grantRepo.mutate()} disabled={!newRepoSlug.trim() || grantRepo.isPending}>
                {t("admin.teams.grantButton")}
              </Button>
            </div>
          </div>
        </CardContent>
      )}
      {dialog}
    </Card>
  );
}


/**
 * Who to add: a member of THIS workspace, found by name or email.
 *
 * This was a free-text box asking for "email or internal id". Internal ids
 * are shown nowhere in the UI, and an email was refused with "Not a member of
 * this workspace" — so the field could only be filled by somebody who had
 * read the database. The list comes from the same people the server accepts
 * (GET /api/teams/{id}/candidates: the workspace's members not yet in the
 * team), searched on the server, so a large workspace is never shown as a
 * silent first slice.
 */
function MemberPicker({
  teamId,
  value,
  onChange,
}: {
  teamId: string;
  value: string;
  onChange: (userId: string) => void;
}) {
  const t = useT();
  const token = useToken();
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(term.trim()), MEMBER_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [term]);

  const candidates = useQuery({
    queryKey: ["teams", teamId, "candidates", debounced],
    queryFn: () => teamsApi.candidates(token!, teamId, debounced),
    enabled: !!token,
    placeholderData: (prev) => prev,
  });

  const rows = candidates.data?.members ?? [];
  const more = Math.max(0, (candidates.data?.total ?? 0) - rows.length);
  const options = rows.map((c) => ({
    value: c.user_id,
    label: c.name || c.email || c.user_id,
    hint: c.name && c.email ? c.email : undefined,
  }));

  return (
    <>
      <div className="space-y-1">
        <Input
          value={term}
          onChange={(e) => {
            setTerm(e.target.value);
            // A new search is a new choice: keep no pick the list may not show.
            onChange("");
          }}
          placeholder={t("admin.teams.memberSearchPlaceholder")}
          aria-label={t("admin.teams.memberSearchLabel")}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="text-xs text-[var(--color-muted-foreground)]" aria-live="polite">
          {candidates.data && rows.length === 0 && (debounced
            ? <p>{t("admin.teams.noCandidateMatches", { q: debounced })}</p>
            : <p>{t("admin.teams.noCandidates")}</p>)}
          {more > 0 && <p>{t("admin.teams.moreCandidates", { count: more })}</p>}
        </div>
      </div>
      <Select
        className="h-11 sm:h-8"
        value={value}
        onChange={onChange}
        options={options}
        disabled={candidates.isLoading || options.length === 0}
        placeholder={t("admin.teams.memberPickPlaceholder")}
      />
    </>
  );
}
