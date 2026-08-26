"use client";

/**
 * /admin/notifications — Grafana-style contact points + event bindings.
 *
 * Channels (Slack / Discord / Google Chat / generic webhook) are shared
 * transports. Bindings say "when EVENT happens on REPO, send to CHANNEL
 * if severity ≥ X".
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BellIcon, PlusIcon, SendIcon, Trash2Icon } from "lucide-react";

import { notificationsApi, type NotificationChannel, type ChannelBinding } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

// Exactly the events something emits. compliance_failed, deprecation_used
// and apply_fix_applied used to be here and were emitted by nothing: the
// binding appeared in the table, read as configured, and was silent for
// ever — and silence is indistinguishable from nothing having gone wrong.
// tests/notifications/test_a_binding_can_only_name_an_event_that_happens.py
// fails if this list and the notify() call sites drift in either direction.
const EVENT_OPTIONS = [
  { v: "*", label: "any event" },
  { v: "review_complete", label: "review_complete" },
  { v: "breaking_change", label: "breaking_change" },
  { v: "agent_turn_done", label: "agent_turn_done" },
  { v: "alert_received", label: "alert_received" },
];

const SEVERITY_OPTIONS = [
  { value: "info", label: "info" },
  { value: "warn", label: "warn" },
  { value: "error", label: "error" },
  { value: "critical", label: "critical" },
];

/** Webhook URLs carry embedded secrets — show origin + tail only. */
function maskWebhookUrl(url: string): string {
  try {
    return `${new URL(url).origin}/…${url.slice(-4)}`;
  } catch {
    return url.length > 12 ? `${url.slice(0, 8)}…${url.slice(-4)}` : url;
  }
}

export default function NotificationsPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();

  const channels = useQuery({
    queryKey: ["notif", "channels"],
    queryFn: () => notificationsApi.listChannels(token!),
    enabled: !!token,
  });
  const bindings = useQuery({
    queryKey: ["notif", "bindings"],
    queryFn: () => notificationsApi.listBindings(token!),
    enabled: !!token,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BellIcon className="h-6 w-6" />}
        title={t("admin.notifications.title")}
        description={t("admin.notifications.pageDescription")}
        tabs={<SectionTabs set="monitoring" />}
      />

      <ChannelCreate onCreated={() => qc.invalidateQueries({ queryKey: ["notif", "channels"] })} />

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.notifications.channelsTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {(channels.data ?? []).length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.notifications.noChannels")}</p>
          )}
          {(channels.data ?? []).map((c) => (
            <ChannelRow key={c.id} c={c}
              onChanged={() => {
                qc.invalidateQueries({ queryKey: ["notif", "channels"] });
                qc.invalidateQueries({ queryKey: ["notif", "bindings"] });
              }}
            />
          ))}
        </CardContent>
      </Card>

      <BindingCreate
        channels={channels.data ?? []}
        onCreated={() => qc.invalidateQueries({ queryKey: ["notif", "bindings"] })}
      />

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.notifications.bindingsTitle")}</CardTitle>
          <CardDescription>{t("admin.notifications.bindingsDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {(bindings.data ?? []).length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.notifications.noBindings")}</p>
          )}
          {(bindings.data ?? []).map((b) => (
            <BindingRow key={b.id} b={b} channels={channels.data ?? []}
              onChanged={() => qc.invalidateQueries({ queryKey: ["notif", "bindings"] })}
            />
          ))}
        </CardContent>
      </Card>
    </PageShell>
  );
}

function ChannelCreate({ onCreated }: { onCreated: () => void }) {
  const t = useT();
  const token = useToken();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"slack" | "discord" | "google_chat" | "webhook">("slack");
  const [url, setUrl] = useState("");

  const create = useMutation({
    mutationFn: () => notificationsApi.createChannel(token!, {
      name: name.trim(), kind, webhook_url: url.trim(), enabled: true, config: {},
    }),
    onSuccess: () => { setName(""); setUrl(""); onCreated(); toast.success(t("admin.notifications.channelCreated")); },
    onError: (e: Error) => toast.error(t("admin.notifications.createFailed", { message: e.message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PlusIcon className="h-4 w-4" /> {t("admin.notifications.newChannel")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 sm:grid-cols-[1fr_auto_2fr_auto] items-end">
        <div>
          <Label>{t("admin.notifications.nameLabel")}</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("admin.notifications.namePlaceholder")} />
        </div>
        <div>
          <Label>{t("admin.notifications.kindLabel")}</Label>
          <Select
            value={kind}
            onChange={(v) => setKind(v as any)}
            options={[
              { value: "slack", label: t("admin.notifications.kindSlack") },
              { value: "discord", label: t("admin.notifications.kindDiscord") },
              { value: "google_chat", label: t("admin.notifications.kindGoogleChat") },
              { value: "webhook", label: t("admin.notifications.kindWebhook") },
            ]}
          />
        </div>
        <div>
          <Label>{t("admin.notifications.webhookUrlLabel")}</Label>
          <Input value={url} onChange={(e) => setUrl(e.target.value)}
                 placeholder="https://hooks.slack.com/..." />
        </div>
        <Button disabled={create.isPending || !name.trim() || url.length < 8}
                onClick={() => create.mutate()}>
          {create.isPending ? "…" : t("admin.notifications.addButton")}
        </Button>
      </CardContent>
    </Card>
  );
}

function ChannelRow({ c, onChanged }: { c: NotificationChannel; onChanged: () => void }) {
  const t = useT();
  const token = useToken();
  const { confirm, dialog } = useConfirm();
  const del = useMutation({
    mutationFn: () => notificationsApi.deleteChannel(token!, c.id),
    onSuccess: () => { toast.success(t("admin.notifications.removed")); onChanged(); },
    onError: (e: Error) => toast.error(e.message),
  });
  const test = useMutation({
    mutationFn: () => notificationsApi.testChannel(token!, c.id),
    onSuccess: (r) => { if (r.ok) toast.success(t("admin.notifications.testSent")); else toast.error(r.detail); },
    onError: (e: Error) => toast.error(t("admin.notifications.testFailed", { message: e.message })),
  });
  return (
    <div className="flex items-center justify-between gap-2 border border-[var(--color-border)] rounded p-2 text-sm">
      <div className="min-w-0">
        <div className="font-medium truncate">{c.name} <Badge variant="outline">{c.kind}</Badge></div>
        <div className="text-xs text-[var(--color-muted-foreground)] font-mono truncate">{maskWebhookUrl(c.webhook_url)}</div>
      </div>
      <div className="flex gap-1 shrink-0">
        <Button variant="ghost" size="sm" onClick={() => test.mutate()} disabled={test.isPending}>
          <SendIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.notifications.testButton")}
        </Button>
        <Button variant="ghost" size="icon" onClick={async () => {
          const ok = await confirm({
            title: t("admin.notifications.deleteChannelConfirm", { name: c.name }),
            confirmLabel: t("common.delete"),
            danger: true,
          });
          if (ok) del.mutate();
        }}>
          <Trash2Icon className="h-4 w-4 text-red-600" />
        </Button>
      </div>
      {dialog}
    </div>
  );
}

function BindingCreate({
  channels, onCreated,
}: { channels: NotificationChannel[]; onCreated: () => void }) {
  const t = useT();
  const token = useToken();
  const [channelId, setChannelId] = useState("");
  const [repoSlug, setRepoSlug] = useState("");
  const [event, setEvent] = useState("*");
  const [minSev, setMinSev] = useState("info");

  const create = useMutation({
    mutationFn: () => notificationsApi.createBinding(token!, {
      channel_id: channelId,
      repo_slug: repoSlug.trim() || null,
      event, min_severity: minSev, enabled: true,
    }),
    onSuccess: () => { setRepoSlug(""); onCreated(); toast.success(t("admin.notifications.bindingCreated")); },
    onError: (e: Error) => toast.error(t("admin.notifications.createFailed", { message: e.message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PlusIcon className="h-4 w-4" /> {t("admin.notifications.newBinding")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 sm:grid-cols-[1.5fr_1.5fr_1fr_1fr_auto] items-end">
        <div>
          <Label>{t("admin.notifications.channelLabel")}</Label>
          <Select
            className="w-full"
            value={channelId} onChange={(v) => setChannelId(v)}
            placeholder={t("admin.notifications.pickOption")}
            options={channels.map((c) => ({ value: c.id, label: `${c.name} (${c.kind})` }))}
          />
        </div>
        <div>
          <Label>{t("admin.notifications.repoSlugLabel")}</Label>
          <Input value={repoSlug} onChange={(e) => setRepoSlug(e.target.value)}
                 placeholder={t("admin.notifications.repoSlugPlaceholder")} />
        </div>
        <div>
          <Label>{t("admin.notifications.eventLabel")}</Label>
          <Select
            className="w-full"
            value={event} onChange={(v) => setEvent(v)}
            options={EVENT_OPTIONS.map((o) => ({
              value: o.v,
              label: o.v === "*" ? t("admin.notifications.anyEvent") : o.label,
            }))}
          />
        </div>
        <div>
          <Label>{t("admin.notifications.minSeverityLabel")}</Label>
          <Select
            className="w-full"
            value={minSev} onChange={(v) => setMinSev(v)}
            options={SEVERITY_OPTIONS}
          />
        </div>
        <Button disabled={!channelId || create.isPending}
                onClick={() => create.mutate()}>
          {create.isPending ? "…" : t("admin.notifications.addButton")}
        </Button>
      </CardContent>
    </Card>
  );
}

function BindingRow({
  b, channels, onChanged,
}: { b: ChannelBinding; channels: NotificationChannel[]; onChanged: () => void }) {
  const t = useT();
  const token = useToken();
  const { confirm, dialog } = useConfirm();
  const del = useMutation({
    mutationFn: () => notificationsApi.deleteBinding(token!, b.id),
    onSuccess: () => { onChanged(); toast.success(t("admin.notifications.removed")); },
    onError: (e: Error) => toast.error(e.message),
  });
  const chan = channels.find((c) => c.id === b.channel_id);
  return (
    <div className="flex items-center justify-between gap-2 border border-[var(--color-border)] rounded p-2 text-sm">
      <div className="min-w-0">
        <span className="font-medium">{chan?.name ?? t("admin.notifications.unknownChannel")}</span>
        {" · "}
        <Badge variant="outline">{b.event}</Badge>
        {" "}
        <Badge variant="outline">{b.min_severity}+</Badge>
        {b.repo_slug && <> {" · "}<code className="text-xs">{b.repo_slug}</code></>}
        {!b.repo_slug && <span className="text-xs text-[var(--color-muted-foreground)]"> · {t("admin.notifications.workspaceWide")}</span>}
      </div>
      <Button
        variant="ghost" size="icon"
        onClick={async () => {
          const ok = await confirm({
            title: t("admin.notifications.deleteBindingConfirm"),
            description: `${chan?.name ?? "?"} · ${b.event} · ${b.min_severity}+`,
            confirmLabel: t("common.delete"),
            danger: true,
          });
          if (ok) del.mutate();
        }}
      >
        <Trash2Icon className="h-4 w-4 text-red-600" />
      </Button>
      {dialog}
    </div>
  );
}
