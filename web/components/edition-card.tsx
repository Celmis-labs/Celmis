"use client";

/**
 * Which edition this API is running, and the licence behind it — on
 * /admin/health.
 *
 * Everyone who reaches the page sees the edition line. A GLOBAL admin also
 * sees the licence in force (customer, features, expiry, where it came from)
 * and can paste a licence key to activate it, or remove the one entered here
 * — `GET/PUT/DELETE /api/license` (src/ee/license_router.py). The API applies
 * it at once, so after a change this card drops the cached capabilities and
 * workspace-role queries: the Analytics tab and the SSO button follow without
 * a reload or a restart.
 *
 * When `CELMIS_LICENSE_KEY` / `CELMIS_LICENSE_FILE` is set on the API, the
 * licence is managed there: the card says so and offers no form (the API
 * would answer 409). A build without src/ee reports `features.license` off,
 * and the form is hidden then too. The token itself is never shown back.
 */
import { useState } from "react";
import { BadgeCheckIcon } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { featureOff, useCapabilities } from "@/lib/use-capabilities";
import { useToken } from "@/lib/use-token";

/** Licence feature name → i18n key; unknown names are shown as they are. */
const FEATURE_KEY: Record<string, string> = {
  sso: "edition.feature.sso",
  analytics: "edition.feature.analytics",
};

/** `source` on GET /api/license → i18n key. */
const SOURCE_KEY: Record<string, string> = {
  env_key: "ee.license.source.env_key",
  env_file: "ee.license.source.env_file",
  ui: "ee.license.source.ui",
};

type LicenseState = {
  edition: string;
  source: string | null;
  managed_by_env: boolean;
  env_variable: string | null;
  problem: string | null;
  license: {
    customer: string;
    features: string[];
    issued_at: string;
    expires_at: string;
    expired: boolean;
  } | null;
};

const LICENSE_KEY = ["license"] as const;

export function EditionCard() {
  const t = useT();
  const caps = useCapabilities();
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.isAdmin);

  const data = caps.data;
  const enterprise = data?.edition === "enterprise";

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <BadgeCheckIcon className="h-4 w-4" aria-hidden />
          {t("edition.title")}
          {data && (
            <Badge variant="outline">
              {enterprise
                ? t("edition.enterprise")
                : data.edition === "community"
                  ? t("edition.community")
                  : data.edition}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!data && !caps.isPending && (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("edition.unknown")}</p>
        )}
        {isAdmin && data && <AdminLicence canManage={!featureOff(data, "license")} />}
      </CardContent>
    </Card>
  );
}

function AdminLicence({ canManage }: { canManage: boolean }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const state = useQuery({
    queryKey: LICENSE_KEY,
    queryFn: () => api<LicenseState>("/api/license", { token }),
    enabled: !!token && canManage,
    retry: false,
  });

  const applied = (next: LicenseState) => {
    qc.setQueryData(LICENSE_KEY, next);
    // Capabilities decide the Analytics tab and the SSO button; the
    // workspace-role query decides who may open Analytics. Both prefixes.
    void qc.invalidateQueries({ queryKey: ["capabilities"] });
    void qc.invalidateQueries({ queryKey: ["workspaces-me"] });
  };
  const failed = (e: unknown) =>
    setError(e instanceof ApiError ? e.message : t("ee.license.errorGeneric"));

  const activate = useMutation({
    mutationFn: () =>
      api<LicenseState>("/api/license", { method: "PUT", token, json: { token: draft.trim() } }),
    onMutate: () => setError(null),
    onSuccess: (next) => {
      applied(next);
      setDraft("");
      toast.success(t("ee.license.activated"));
    },
    onError: failed,
  });

  const remove = useMutation({
    mutationFn: () => api<LicenseState>("/api/license", { method: "DELETE", token }),
    onMutate: () => setError(null),
    onSuccess: (next) => {
      applied(next);
      toast.success(t("ee.license.removed"));
    },
    onError: failed,
  });

  const s = state.data;
  const lic = s?.license ?? null;

  return (
    <div className="space-y-3">
      {lic ? (
        <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[max-content_1fr]">
          <dt className="text-[var(--color-muted-foreground)]">{t("edition.customer")}</dt>
          <dd>{lic.customer}</dd>
          <dt className="text-[var(--color-muted-foreground)]">{t("edition.expires")}</dt>
          <dd className={lic.expired ? "font-semibold text-red-600" : undefined}>
            {formatDate(lic.expires_at)}
            {lic.expired && ` — ${t("edition.expired")}`}
          </dd>
          <dt className="text-[var(--color-muted-foreground)]">{t("edition.features")}</dt>
          <dd>
            {lic.features.length
              ? lic.features.map((f) => (FEATURE_KEY[f] ? t(FEATURE_KEY[f]) : f)).join(", ")
              : "—"}
          </dd>
          <dt className="text-[var(--color-muted-foreground)]">{t("ee.license.issued")}</dt>
          <dd>{formatDate(lic.issued_at)}</dd>
          {s?.source && (
            <>
              <dt className="text-[var(--color-muted-foreground)]">{t("ee.license.sourceLabel")}</dt>
              <dd>{SOURCE_KEY[s.source] ? t(SOURCE_KEY[s.source]) : s.source}</dd>
            </>
          )}
        </dl>
      ) : (
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("edition.communityHint")}</p>
      )}

      {s?.problem && (
        <Callout tone="warning">{t("ee.license.problem", { reason: s.problem })}</Callout>
      )}

      {!canManage ? null : s?.managed_by_env ? (
        <Callout tone="info">
          {t("ee.license.managedByEnv", { variable: s.env_variable ?? "CELMIS_LICENSE_KEY" })}
        </Callout>
      ) : s ? (
        <div className="space-y-2">
          <label htmlFor="licence-key" className="block text-xs font-medium">
            {lic ? t("ee.license.replaceLabel") : t("ee.license.pasteLabel")}
          </label>
          <Textarea
            id="licence-key"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t("ee.license.placeholder")}
            spellCheck={false}
            autoComplete="off"
            className="font-mono text-xs"
            rows={3}
          />
          {error && <Callout tone="danger">{error}</Callout>}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={!draft.trim() || activate.isPending}
              onClick={() => activate.mutate()}
            >
              {activate.isPending ? t("ee.license.activating") : t("ee.license.activate")}
            </Button>
            {s.source === "ui" && (
              <Button
                size="sm"
                variant="outline"
                disabled={remove.isPending}
                onClick={async () => {
                  if (
                    await confirm({
                      title: t("ee.license.removeConfirmTitle"),
                      description: t("ee.license.removeConfirmBody"),
                      confirmLabel: t("ee.license.remove"),
                      danger: true,
                    })
                  ) {
                    remove.mutate();
                  }
                }}
              >
                {t("ee.license.remove")}
              </Button>
            )}
          </div>
        </div>
      ) : state.isError ? (
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("ee.license.loadFailed")}</p>
      ) : null}
      {dialog}
    </div>
  );
}
