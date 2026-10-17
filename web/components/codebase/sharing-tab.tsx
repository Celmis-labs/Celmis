"use client";

/**
 * Who a pack is shown to and who may change it — maintainers only.
 *
 * Five things, in the order a maintainer reaches for them: the audience (the
 * wide setting), the teams it is granted to, the people added by name, invite
 * links for the ones who cannot find it, and publish tokens for the scripts
 * that push new versions (`celmis-pack`). The API refuses all of this to a
 * reader, so a pack the person only reads has no Sharing tab at all.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRoundIcon, LinkIcon, MailPlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import {
  codebaseApi, teamsApi, type PackAudience, type PackDetail, type PackInvite,
  type PackInviteCreated, type PackPermission, type PackPublishToken,
  type PackPublishTokenCreated, publicMcpUrl,
} from "@/lib/api";
import {
  inviteState, PACK_PERMISSIONS, PUBLISH_TOKEN_LIMITS, publishCommands, tokenState,
  type InviteState,
} from "@/lib/codebase";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { AudienceBadge, AudienceGuide, AudiencePicker } from "@/components/codebase/audience";
import { CodeBlock } from "@/components/codebase/copy";
import { TeamGrantsEditor, type TeamGrant } from "@/components/codebase/team-grants";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

export function SharingTab({ pack }: { pack: PackDetail }) {
  return (
    <div className="space-y-4">
      <AudienceCard pack={pack} />
      <TeamsCard pack={pack} />
      <PeopleCard pack={pack} />
      <InvitesCard pack={pack} />
      <PublishTokensCard pack={pack} />
    </div>
  );
}

function AudienceCard({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<PackAudience | null>(null);
  const value = draft ?? pack.audience;

  const save = useMutation({
    mutationFn: () => codebaseApi.update(token!, pack.id, { audience: value }),
    onSuccess: () => {
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.sharing.audienceSaved"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          {t("codebase.sharing.audienceTitle")} <AudienceBadge audience={pack.audience} teamCount={pack.teams.length} />
        </CardTitle>
        <CardDescription>{t("codebase.sharing.audienceDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <AudiencePicker
          value={value} onChange={setDraft} disabled={save.isPending} teamCount={pack.teams.length}
        />
        <div>
          <Button
            size="sm" loading={save.isPending}
            disabled={value === pack.audience}
            onClick={() => save.mutate()}
          >
            {t("common.save")}
          </Button>
        </div>
        <div className="border-t border-[var(--color-border)] pt-4">
          <div className="mb-2 text-sm font-medium">{t("codebase.guide.title")}</div>
          <AudienceGuide current={value} />
        </div>
      </CardContent>
    </Card>
  );
}

function TeamsCard({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const teams = useQuery({
    queryKey: ["codebase", "teams"],
    queryFn: () => teamsApi.list(token!),
    enabled: !!token,
  });
  const saved: TeamGrant[] = pack.teams.map((g) => ({ team_id: g.id, permission: g.permission }));
  const [draft, setDraft] = useState<TeamGrant[] | null>(null);
  const value = draft ?? saved;
  const names = Object.fromEntries(pack.teams.map((g) => [g.id, g.name]));
  const key = (list: TeamGrant[]) =>
    JSON.stringify([...list].sort((a, b) => a.team_id.localeCompare(b.team_id)));
  const dirty = key(value) !== key(saved);

  const save = useMutation({
    mutationFn: () => codebaseApi.setTeams(token!, pack.id, value),
    onSuccess: () => {
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.sharing.teamsSaved"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{t("codebase.sharing.teamsTitle")}</CardTitle>
        <CardDescription>{t("codebase.sharing.teamsDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <TeamGrantsEditor
          value={value} onChange={setDraft} teams={teams.data ?? []} names={names}
          disabled={save.isPending}
        />
        <Button size="sm" loading={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>
          {t("common.save")}
        </Button>
      </CardContent>
    </Card>
  );
}

function PeopleCard({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [email, setEmail] = useState("");
  const [permission, setPermission] = useState<PackPermission>("read");

  const add = useMutation({
    mutationFn: () => codebaseApi.addMember(token!, pack.id, email.trim(), permission),
    onSuccess: () => {
      setEmail("");
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.sharing.personAdded"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });
  const remove = useMutation({
    mutationFn: (userId: string) => codebaseApi.removeMember(token!, pack.id, userId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.sharing.personRemoved"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{t("codebase.sharing.peopleTitle")}</CardTitle>
        <CardDescription>{t("codebase.sharing.peopleDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {pack.members.length === 0 ? (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.sharing.peopleNone")}</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)] rounded-lg border border-[var(--color-border)]">
            {pack.members.map((m) => (
              <li key={m.user_id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-sm">{m.email ?? m.user_id}</span>
                <Badge variant={m.permission === "maintain" ? "brand" : "outline"}>
                  {t(`codebase.permission.${m.permission}`)}
                </Badge>
                {m.via_invite && <Badge variant="info">{t("codebase.sharing.viaInvite")}</Badge>}
                <Button
                  size="icon-sm" variant="ghost" disabled={remove.isPending}
                  aria-label={t("codebase.sharing.removePerson", { name: m.email ?? m.user_id })}
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("codebase.sharing.confirmRemove", { name: m.email ?? m.user_id }),
                      description: t("codebase.sharing.confirmRemoveDesc"),
                      confirmLabel: t("common.remove"),
                      danger: true,
                    });
                    if (ok) remove.mutate(m.user_id);
                  }}
                >
                  <Trash2Icon />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => { e.preventDefault(); if (email.trim()) add.mutate(); }}
        >
          <div className="min-w-0 flex-1 space-y-1.5 sm:max-w-xs">
            <Label htmlFor="pack-person">{t("codebase.sharing.personEmail")}</Label>
            <Input
              id="pack-person" type="email" value={email} maxLength={320}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={t("codebase.sharing.personPlaceholder")}
            />
          </div>
          <Select
            value={permission} className="w-36"
            options={PACK_PERMISSIONS.map((p) => ({ value: p, label: t(`codebase.permission.${p}`) }))}
            onChange={(p) => setPermission(p as PackPermission)}
          />
          <Button type="submit" size="sm" loading={add.isPending} disabled={!email.trim()}>
            <MailPlusIcon />{t("common.add")}
          </Button>
        </form>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.sharing.peopleHint")}</p>
      </CardContent>
      {dialog}
    </Card>
  );
}

const EXPIRY_DAYS = [1, 3, 7, 14, 30];
const USE_LIMITS = [1, 5, 25, 100, 500];

const INVITE_BADGE: Record<InviteState, BadgeVariant> = {
  active: "success", revoked: "outline", expired: "outline", used: "warning",
};

function InvitesCard({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [days, setDays] = useState(7);
  const [uses, setUses] = useState(25);
  // When the card was drawn, for the status of each link. Held in state so a
  // render stays a pure function of its inputs.
  const [now] = useState(() => Date.now());
  // The link exists only here: the server keeps a hash, so once this card
  // unmounts there is nothing left to show.
  const [fresh, setFresh] = useState<{ url: string; invite: PackInviteCreated["invite"] } | null>(null);

  const invites = useQuery({
    queryKey: ["codebase", "invites", pack.id],
    queryFn: () => codebaseApi.invites(token!, pack.id),
    enabled: !!token,
  });
  const create = useMutation({
    mutationFn: () => codebaseApi.createInvite(token!, pack.id, { expires_in_days: days, max_uses: uses }),
    onSuccess: (r) => {
      setFresh({ url: `${window.location.origin}${r.path}`, invite: r.invite });
      qc.invalidateQueries({ queryKey: ["codebase", "invites", pack.id] });
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => codebaseApi.revokeInvite(token!, pack.id, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["codebase", "invites", pack.id] });
      toast.success(t("codebase.sharing.inviteRevoked"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });

  const rows: PackInvite[] = invites.data?.invites ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <LinkIcon aria-hidden className="size-4" /> {t("codebase.sharing.invitesTitle")}
        </CardTitle>
        <CardDescription>{t("codebase.sharing.invitesDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="invite-days">{t("codebase.sharing.inviteExpires")}</Label>
            <Select
              id="invite-days" value={String(days)} className="w-36"
              options={EXPIRY_DAYS.map((d) => ({ value: String(d), label: t("codebase.sharing.days", { count: d }) }))}
              onChange={(v) => setDays(Number(v))}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invite-uses">{t("codebase.sharing.inviteUses")}</Label>
            <Select
              id="invite-uses" value={String(uses)} className="w-36"
              options={USE_LIMITS.map((n) => ({ value: String(n), label: t("codebase.sharing.uses", { count: n }) }))}
              onChange={(v) => setUses(Number(v))}
            />
          </div>
          <Button size="sm" loading={create.isPending} onClick={() => create.mutate()}>
            <LinkIcon />{t("codebase.sharing.inviteCreate")}
          </Button>
        </div>

        {fresh && (
          <Callout tone="success" className="space-y-2">
            <p className="font-medium">{t("codebase.sharing.inviteReady")}</p>
            <p>{t("codebase.sharing.inviteOnce", { date: formatDate(fresh.invite.expires_at) })}</p>
            <CodeBlock text={fresh.url} label={t("codebase.sharing.inviteCopy")} wrap />
          </Callout>
        )}

        {rows.length > 0 && (
          <Table>
            <THead>
              <TR>
                <TH>{t("codebase.sharing.inviteCreated")}</TH>
                <TH>{t("codebase.sharing.inviteExpiry")}</TH>
                <TH>{t("codebase.sharing.inviteUsed")}</TH>
                <TH>{t("codebase.sharing.inviteStatus")}</TH>
                <TH><span className="sr-only">{t("codebase.versions.actions")}</span></TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((invite) => {
                const state = inviteState(invite, now);
                return (
                  <TR key={invite.id}>
                    <TD className="whitespace-nowrap">
                      {formatDate(invite.created_at)}
                      {invite.created_by && (
                        <div className="text-[11px] text-[var(--color-muted-foreground)]">{invite.created_by}</div>
                      )}
                    </TD>
                    <TD className="whitespace-nowrap">{formatDate(invite.expires_at)}</TD>
                    <TD className="whitespace-nowrap">{invite.used_count} / {invite.max_uses}</TD>
                    <TD><Badge variant={INVITE_BADGE[state]}>{t(`codebase.sharing.state.${state}`)}</Badge></TD>
                    <TD className="text-right">
                      {state === "active" && (
                        <Button
                          size="xs" variant="outline" disabled={revoke.isPending}
                          onClick={async () => {
                            const ok = await confirm({
                              title: t("codebase.sharing.confirmRevoke"),
                              description: t("codebase.sharing.confirmRevokeDesc"),
                              confirmLabel: t("codebase.sharing.revoke"),
                              danger: true,
                            });
                            if (ok) revoke.mutate(invite.id);
                          }}
                        >
                          {t("codebase.sharing.revoke")}
                        </Button>
                      )}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
        {invites.isSuccess && rows.length === 0 && (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.sharing.invitesNone")}</p>
        )}
        {invites.isError && <Callout tone="danger">{(invites.error as Error).message}</Callout>}
      </CardContent>
      {dialog}
    </Card>
  );
}

const TOKEN_DAYS = [30, 90, 180, 365];

function PublishTokensCard({ pack }: { pack: PackDetail }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [label, setLabel] = useState("");
  const [days, setDays] = useState<number>(PUBLISH_TOKEN_LIMITS.defaultDays);
  const [now] = useState(() => Date.now());
  // The token exists only here: the server keeps a hash, so once this card
  // unmounts there is nothing left to show.
  const [fresh, setFresh] = useState<PackPublishTokenCreated | null>(null);

  const tokens = useQuery({
    queryKey: ["codebase", "tokens", pack.id],
    queryFn: () => codebaseApi.publishTokens(token!, pack.id),
    enabled: !!token,
  });
  const create = useMutation({
    mutationFn: () => codebaseApi.createPublishToken(token!, pack.id, {
      label: label.trim(), expires_in_days: days,
    }),
    onSuccess: (r) => {
      setFresh(r);
      setLabel("");
      qc.invalidateQueries({ queryKey: ["codebase", "tokens", pack.id] });
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => codebaseApi.revokePublishToken(token!, pack.id, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["codebase", "tokens", pack.id] });
      toast.success(t("codebase.tokens.revoked"));
    },
    onError: (e) => toast.error(t("codebase.sharing.saveFailed", { message: (e as Error).message })),
  });

  const rows: PackPublishToken[] = tokens.data?.tokens ?? [];
  const active = rows.filter((r) => tokenState(r, now) === "active").length;
  const full = active >= PUBLISH_TOKEN_LIMITS.maxActive;
  const address = typeof window !== "undefined" ? publicMcpUrl("") : "";

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <KeyRoundIcon aria-hidden className="size-4" /> {t("codebase.tokens.title")}
        </CardTitle>
        <CardDescription>{t("codebase.tokens.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => { e.preventDefault(); if (label.trim() && !full) create.mutate(); }}
        >
          <div className="min-w-0 flex-1 space-y-1.5 sm:max-w-xs">
            <Label htmlFor="token-label">{t("codebase.tokens.label")}</Label>
            <Input
              id="token-label" value={label} maxLength={PUBLISH_TOKEN_LIMITS.labelChars}
              onChange={(e) => setLabel(e.target.value)}
              placeholder={t("codebase.tokens.labelPlaceholder")}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="token-days">{t("codebase.sharing.inviteExpires")}</Label>
            <Select
              id="token-days" value={String(days)} className="w-36"
              options={TOKEN_DAYS.map((d) => ({ value: String(d), label: t("codebase.sharing.days", { count: d }) }))}
              onChange={(v) => setDays(Number(v))}
            />
          </div>
          <Button type="submit" size="sm" loading={create.isPending} disabled={!label.trim() || full}>
            <KeyRoundIcon />{t("codebase.tokens.create")}
          </Button>
        </form>
        {full && (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("codebase.tokens.limit", { count: PUBLISH_TOKEN_LIMITS.maxActive })}
          </p>
        )}

        {fresh && (
          <Callout tone="success" className="space-y-2">
            <p className="font-medium">{t("codebase.tokens.ready")}</p>
            <p>{t("codebase.tokens.once", { date: formatDate(fresh.token_info.expires_at) })}</p>
            <CodeBlock text={fresh.token} label={t("codebase.tokens.copyToken")} wrap />
            <p>{t("codebase.tokens.howTo")}</p>
            <CodeBlock
              text={publishCommands(address, pack.slug, t("codebase.tokens.pasteToken"))}
              label={t("codebase.tokens.copyCommands")}
            />
            <p className="text-xs">{t("codebase.tokens.keychain")}</p>
          </Callout>
        )}

        {rows.length > 0 && (
          <Table>
            <THead>
              <TR>
                <TH>{t("codebase.tokens.label")}</TH>
                <TH>{t("codebase.sharing.inviteExpiry")}</TH>
                <TH>{t("codebase.tokens.lastUsed")}</TH>
                <TH>{t("codebase.sharing.inviteStatus")}</TH>
                <TH><span className="sr-only">{t("codebase.versions.actions")}</span></TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((row) => {
                const state = tokenState(row, now);
                return (
                  <TR key={row.id}>
                    <TD className="break-words">
                      {row.label}
                      <div className="text-[11px] text-[var(--color-muted-foreground)]">
                        {formatDate(row.created_at)}{row.created_by ? ` · ${row.created_by}` : ""}
                      </div>
                    </TD>
                    <TD className="whitespace-nowrap">{formatDate(row.expires_at)}</TD>
                    <TD className="whitespace-nowrap">
                      {row.last_used_at ? formatDate(row.last_used_at) : t("codebase.usage.never")}
                    </TD>
                    <TD><Badge variant={INVITE_BADGE[state]}>{t(`codebase.sharing.state.${state}`)}</Badge></TD>
                    <TD className="text-right">
                      {state === "active" && (
                        <Button
                          size="xs" variant="outline" disabled={revoke.isPending}
                          onClick={async () => {
                            const ok = await confirm({
                              title: t("codebase.tokens.confirmRevoke"),
                              description: t("codebase.tokens.confirmRevokeDesc"),
                              confirmLabel: t("codebase.sharing.revoke"),
                              danger: true,
                            });
                            if (ok) revoke.mutate(row.id);
                          }}
                        >
                          {t("codebase.sharing.revoke")}
                        </Button>
                      )}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
        {tokens.isSuccess && rows.length === 0 && (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.tokens.none")}</p>
        )}
        {tokens.isError && <Callout tone="danger">{(tokens.error as Error).message}</Callout>}
      </CardContent>
      {dialog}
    </Card>
  );
}
