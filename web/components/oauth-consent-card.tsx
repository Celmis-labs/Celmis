"use client";

/**
 * What a person reads before letting an app into Celmis: who is asking, where
 * they are sent afterwards, what it can do in plain words, in which workspace.
 * Presentational only; the page (app/oauth/consent) owns the data and the calls.
 *
 * The app's name is whatever the client called itself when it registered, so
 * it is shown as text (never as markup) and always next to the host the browser
 * will be sent to, which the client cannot choose freely.
 */

import { ShieldCheckIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/lib/i18n";
import { isWriteScope, scopeKey, type ConsentInfo } from "@/lib/oauth-consent";

export interface OAuthConsentCardProps {
  info: ConsentInfo;
  /** The slug of the workspace the grant is for. */
  workspace: string;
  onWorkspace: (slug: string) => void;
  busy: boolean;
  error: string | null;
  onAllow: () => void;
  onDeny: () => void;
}

export function OAuthConsentCard(p: OAuthConsentCardProps) {
  const t = useT();
  const { info } = p;
  const granted = info.scopes.filter((s) => s.granted);
  const withheld = info.scopes.filter((s) => !s.granted);
  const canAllow = !info.blocked && !p.busy;

  return (
    <Card className="w-full max-w-lg" data-testid="oauth-consent">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <ShieldCheckIcon className="h-5 w-5" />
          {t("oauth.consent.title", { app: info.client.name })}
        </CardTitle>
        <CardDescription>
          {t("oauth.consent.redirects", { host: info.client.redirect_host })}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <div className="mb-1 text-sm font-medium">{t("oauth.consent.canDo")}</div>
          <ul className="list-disc space-y-1 pl-5 text-sm" data-testid="oauth-scopes">
            {granted.map((s) => (
              <li key={s.scope}>
                {t(scopeKey(s.scope))}
                {isWriteScope(s.scope) && (
                  <strong className="ml-1 text-[var(--color-warning)]">{t("oauth.consent.changes")}</strong>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-[var(--color-muted-foreground)]">
            {t("oauth.consent.ownAccess")}
          </p>
        </div>

        {withheld.length > 0 && (
          <Callout tone="warning">
            <div className="font-medium">{t("oauth.consent.withheld")}</div>
            <ul className="mt-1 list-disc pl-5">
              {withheld.map((s) => (
                <li key={s.scope}>{t(scopeKey(s.scope))}{s.reason ? ` (${s.reason})` : ""}</li>
              ))}
            </ul>
          </Callout>
        )}

        {info.workspaces.length > 1 ? (
          <label className="block text-sm">
            <span className="mb-1 block font-medium">{t("oauth.consent.workspace")}</span>
            <select
              className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2 py-1.5 text-sm"
              value={p.workspace}
              disabled={p.busy}
              onChange={(e) => p.onWorkspace(e.target.value)}
              data-testid="oauth-workspace"
            >
              {info.workspaces.map((w) => (
                <option key={w.id} value={w.slug}>{w.name}</option>
              ))}
            </select>
          </label>
        ) : (
          <div className="text-sm">
            <span className="font-medium">{t("oauth.consent.workspace")}: </span>
            {info.workspace.name}
          </div>
        )}

        {!info.client.registered_by_administrator && (
          <Callout tone="info">{t("oauth.consent.unverified")}</Callout>
        )}
        {info.blocked && <Callout tone="danger">{info.blocked}</Callout>}
        {p.error && <Callout tone="danger">{p.error}</Callout>}

        <div className="flex gap-2 pt-1">
          <Button onClick={p.onAllow} disabled={!canAllow} data-testid="oauth-allow">
            {t("oauth.consent.allow")}
          </Button>
          <Button variant="outline" onClick={p.onDeny} disabled={p.busy} data-testid="oauth-deny">
            {t("oauth.consent.deny")}
          </Button>
        </div>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("oauth.consent.revokeLater")}</p>
      </CardContent>
    </Card>
  );
}
