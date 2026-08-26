"use client";

/**
 * /admin/oauth-clients — OAuth 2.1 client registry.
 *
 * Public (PKCE) clients — no secret returned. Confidential clients get
 * a one-time secret display; we won't have it again after this modal
 * closes, so users must copy it now.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { KeyIcon, PlusIcon, Trash2Icon } from "lucide-react";

import {
  oauthClientsApi, type OAuthClientRegistered, type OAuthClientSummary,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { AdminGate } from "@/components/admin-gate";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Card, CardContent, CardHeader, CardTitle,
} from "@/components/ui/card";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

export default function OAuthClientsPage() {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const [justCreated, setJustCreated] = useState<OAuthClientRegistered | null>(null);

  const list = useQuery({
    queryKey: ["oauth-clients"],
    queryFn: () => oauthClientsApi.list(token!),
    enabled: !!token,
  });

  const del = useMutation({
    mutationFn: (id: string) => oauthClientsApi.remove(token!, id),
    onSuccess: () => {
      toast.success(t("admin.oauthClients.deleteSuccess"));
      qc.invalidateQueries({ queryKey: ["oauth-clients"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <AdminGate>
    <PageShell width="wide">
      <PageHeader
        icon={<KeyIcon className="h-6 w-6" />}
        title={t("admin.oauthClients.title")}
        description={
          <>
            {t("admin.oauthClients.description")}{" "}
            <code>/oauth/authorize</code>{" "}
            → <code>/oauth/token</code>.{" "}
            {t("admin.oauthClients.descriptionPublic")}
          </>
        }
        tabs={<SectionTabs set="admin" />}
      />

      <Register onCreated={(c) => {
        setJustCreated(c);
        qc.invalidateQueries({ queryKey: ["oauth-clients"] });
      }} />

      {/* One-time secret display: modal instead of an inline card, so the
          "copy it now" moment is impossible to scroll past. */}
      <Dialog
        open={!!justCreated}
        onOpenChange={(open) => { if (!open) setJustCreated(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("admin.oauthClients.registeredTitle")}</DialogTitle>
            <DialogDescription>
              {t("admin.oauthClients.registeredDescription")}
            </DialogDescription>
          </DialogHeader>
          <Callout tone="warning">{t("admin.oauthClients.secretWarning")}</Callout>
          <div className="space-y-2 break-all text-xs font-mono">
            <div><b>client_id:</b> {justCreated?.client_id}</div>
            {justCreated?.client_secret ? (
              <div><b>client_secret:</b> {justCreated.client_secret}</div>
            ) : (
              <div><Badge variant="outline">{t("admin.oauthClients.noSecretBadge")}</Badge></div>
            )}
          </div>
          <div className="flex justify-end">
            <Button variant="outline" size="sm" onClick={() => setJustCreated(null)}>
              {t("admin.oauthClients.close")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Card>
        <CardHeader><CardTitle>{t("admin.oauthClients.registeredClients")}</CardTitle></CardHeader>
        <CardContent className="space-y-2">
          {list.isLoading && <div className="text-sm">{t("admin.oauthClients.loading")}</div>}
          {(list.data ?? []).length === 0 && !list.isLoading && (
            <div className="text-sm text-[var(--color-muted-foreground)]">
              {t("admin.oauthClients.emptyState")}
            </div>
          )}
          {(list.data ?? []).map((c) => (
            <ClientRow key={c.client_id} c={c}
              onDelete={async () => {
                const ok = await confirm({
                  title: t("admin.oauthClients.deleteConfirm", { name: c.name }),
                  confirmLabel: t("common.delete"),
                  danger: true,
                });
                if (ok) del.mutate(c.client_id);
              }}
            />
          ))}
        </CardContent>
      </Card>
      {dialog}
    </PageShell>
    </AdminGate>
  );
}


// Every scope the MCP/OAuth surface understands, with a one-line meaning.
const KNOWN_SCOPES: { value: string; descKey: string }[] = [
  { value: "read:graph", descKey: "admin.oauthClients.scope.readGraph" },
  { value: "read:groups", descKey: "admin.oauthClients.scope.readGroups" },
  { value: "write:groups", descKey: "admin.oauthClients.scope.writeGroups" },
  { value: "read:reviews", descKey: "admin.oauthClients.scope.readReviews" },
  { value: "write:reviews", descKey: "admin.oauthClients.scope.writeReviews" },
  { value: "write:policies", descKey: "admin.oauthClients.scope.writePolicies" },
  { value: "review:pr", descKey: "admin.oauthClients.scope.reviewPr" },
  { value: "admin", descKey: "admin.oauthClients.scope.admin" },
];

function ScopePicker({
  selected, onChange,
}: {
  selected: string[]; onChange: (next: string[]) => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const visible = KNOWN_SCOPES.filter((s) =>
    s.value.includes(query.toLowerCase()) ||
    t(s.descKey).toLowerCase().includes(query.toLowerCase()));
  const toggle = (v: string) =>
    onChange(selected.includes(v) ? selected.filter((s) => s !== v) : [...selected, v]);
  return (
    <div className="space-y-2">
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((s) => (
            <button key={s} type="button" onClick={() => toggle(s)}
              className="inline-flex items-center gap-1 rounded-full border border-[var(--color-brand)]/50 bg-[var(--color-brand-muted)] px-2 py-0.5 text-xs">
              {s} <span className="opacity-60">×</span>
            </button>
          ))}
        </div>
      )}
      <Input value={query} onChange={(e) => setQuery(e.target.value)}
        placeholder={t("admin.oauthClients.scopeSearch")} />
      <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">
        {visible.map((s) => (
          <label key={s.value} className="flex cursor-pointer items-start gap-2 rounded px-1 py-0.5 text-sm hover:bg-[var(--color-accent)]">
            <input type="checkbox" className="mt-1" checked={selected.includes(s.value)}
              onChange={() => toggle(s.value)} />
            <span>
              <code className="text-xs">{s.value}</code>
              <span className="block text-xs text-[var(--color-muted-foreground)]">{t(s.descKey)}</span>
            </span>
          </label>
        ))}
        {visible.length === 0 && (
          <div className="px-1 py-2 text-xs text-[var(--color-muted-foreground)]">
            {t("admin.oauthClients.scopeNoMatch")}
          </div>
        )}
      </div>
    </div>
  );
}

function Register({ onCreated }: { onCreated: (c: OAuthClientRegistered) => void }) {
  const token = useToken();
  const t = useT();
  const [name, setName] = useState("");
  const [uris, setUris] = useState("http://localhost:9999/cb");
  const [scopeList, setScopeList] = useState<string[]>(["read:reviews"]);
  const [isPublic, setIsPublic] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);

  const create = useMutation({
    mutationFn: () => oauthClientsApi.register(token!, {
      name: name.trim(),
      redirect_uris: uris.split(",").map((s) => s.trim()).filter(Boolean),
      allowed_scopes: scopeList,
      public: isPublic,
    }),
    onSuccess: (c) => { setName(""); onCreated(c); toast.success(t("admin.oauthClients.registerSuccess")); },
    onError: (e: Error) => toast.error(t("admin.oauthClients.registerError", { message: e.message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PlusIcon className="h-4 w-4" /> {t("admin.oauthClients.registerClient")}
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("admin.oauthClients.helpTitle")} />
        </CardTitle>
      </CardHeader>

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("admin.oauthClients.helpTitle")}</DialogTitle>
            <DialogDescription>{t("admin.oauthClients.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 text-sm">
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpWhatTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.oauthClients.helpWhatBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpFlowTitle")}</div>
              <ol className="list-decimal space-y-1 pl-5 text-xs text-[var(--color-muted-foreground)]">
                <li>{t("admin.oauthClients.helpFlow1")}</li>
                <li>{t("admin.oauthClients.helpFlow2")}</li>
                <li>{t("admin.oauthClients.helpFlow3")}</li>
                <li>{t("admin.oauthClients.helpFlow4")}</li>
              </ol>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpTypesTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.oauthClients.helpTypesBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpRedirectTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.oauthClients.helpRedirectBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpScopesTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.oauthClients.helpScopesBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.oauthClients.helpExampleTitle")}</div>
              <pre className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-3 py-2 text-[11px] leading-relaxed">
{`claude mcp add celmis \\
  --transport http \\
  http://<host>/backend/mcp`}
              </pre>
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">{t("admin.oauthClients.helpExampleBody")}</p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <CardContent className="space-y-2">
        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <Label>{t("admin.oauthClients.nameLabel")}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t("admin.oauthClients.namePlaceholder")} />
          </div>
          <div>
            <Label>{t("admin.oauthClients.clientTypeLabel")}</Label>
            <Select
              className="w-full"
              value={isPublic ? "public" : "confidential"}
              onChange={(v) => setIsPublic(v === "public")}
              options={[
                { value: "public", label: t("admin.oauthClients.optionPublic") },
                { value: "confidential", label: t("admin.oauthClients.optionConfidential") },
              ]}
            />
          </div>
        </div>
        <div>
          <Label>{t("admin.oauthClients.redirectUrisLabel")}</Label>
          <Input value={uris} onChange={(e) => setUris(e.target.value)} />
        </div>
        <div>
          <Label>{t("admin.oauthClients.allowedScopesLabel")}</Label>
          <ScopePicker selected={scopeList} onChange={setScopeList} />
        </div>
        <div className="flex justify-end">
          <Button disabled={!name.trim() || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? t("admin.oauthClients.registering") : t("admin.oauthClients.registerButton")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}


function ClientRow({ c, onDelete }: { c: OAuthClientSummary; onDelete: () => void }) {
  const t = useT();
  return (
    <div className="border border-[var(--color-border)] rounded p-2 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{c.name}</span>
            <Badge variant="outline">{c.is_public ? t("admin.oauthClients.publicLabel") : t("admin.oauthClients.confidentialLabel")}</Badge>
            <code className="text-xs">{c.client_id}</code>
          </div>
          <div className="text-xs text-[var(--color-muted-foreground)] mt-1">
            {(c.redirect_uris || []).map((u) => (
              <div key={u}>↪ <code>{u}</code></div>
            ))}
          </div>
          <div className="text-xs mt-1 flex flex-wrap gap-1">
            {(c.allowed_scopes || []).map((s) => (
              <Badge key={s} variant="outline" className="font-mono text-[9px]">{s}</Badge>
            ))}
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={onDelete}>
          <Trash2Icon className="h-4 w-4 text-red-600" />
        </Button>
      </div>
    </div>
  );
}
