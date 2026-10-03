"use client";

/**
 * The invite landing page itself (page.tsx is its server wrapper).
 *
 * Works for both signed-in and signed-out visitors: the preview is public so
 * the page can say what the link grants — workspace, role, who sent it —
 * before asking anyone to log in. A signed-out visitor picks how to sign in
 * (password, sign-up, Google, company SSO) and every one of them comes back
 * here (`callbackUrl` / `next`) to accept.
 *
 * Google and SSO sign-ins with the invited, provider-verified address have
 * usually joined already by the time they land back here (the API redeems
 * those at sign-in); the link then answers "already joined" and opens the
 * workspace. A password account is never joined by its address alone — it
 * accepts here.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getProviders, signIn, signOut, useSession } from "next-auth/react";
import { toast } from "sonner";
import { BuildingIcon, CheckIcon, LogInIcon, UserPlusIcon, XCircleIcon } from "lucide-react";

import { invitesApi } from "@/lib/api";
import { forgetAgentSession } from "@/lib/agent-session";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { roleLabel } from "@/lib/roles";
import { SsoButton } from "@/ee/sso/sso-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const REASON_KEYS: Record<string, string> = {
  not_found: "invite.reason.not_found",
  revoked: "invite.reason.revoked",
  expired: "invite.reason.expired",
  used: "invite.reason.used",
};

export function InviteView({
  inviteToken,
  passwordLogin,
  ssoName,
}: {
  inviteToken: string;
  passwordLogin: boolean;
  /** Company SSO display name, or null when SSO is not offered (decided on
   *  the server, same rule as /login). */
  ssoName: string | null;
}) {
  const t = useT();
  const jwt = useToken();
  const { data: session, status } = useSession();
  const [busy, setBusy] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const [googleEnabled, setGoogleEnabled] = useState(false);

  useEffect(() => {
    getProviders()
      .then((providers) => setGoogleEnabled(Boolean(providers && "google" in providers)))
      .catch(() => setGoogleEnabled(false));
  }, []);

  const preview = useQuery({
    queryKey: ["invite", inviteToken],
    queryFn: () => invitesApi.preview(inviteToken),
  });

  const back = `/invite/${inviteToken}`;

  const accept = async () => {
    if (!jwt) return;
    setBusy(true);
    setAcceptError(null);
    try {
      const r = await invitesApi.accept(jwt, inviteToken);
      // Switch the active workspace to the one we just joined. Same contract
      // as the workspace switcher (lib/agent-session.ts): drop the agent's
      // session from the previous workspace and finish with a full page load,
      // so neither the stored session id nor any in-memory cache carries over.
      document.cookie = `x-workspace=${r.workspace_slug}; path=/; max-age=31536000; SameSite=Lax`;
      forgetAgentSession();
      toast.success(t("invite.joined", { workspace: r.workspace_slug, role: roleLabel(t, r.role) }));
      window.location.assign("/dashboard");
    } catch (err) {
      const message = (err as Error).message;
      setAcceptError(message);
      toast.error(message);
    } finally {
      setBusy(false);
    }
  };

  const p = preview.data;
  const reasonText = p && !p.valid
    ? t(REASON_KEYS[p.reason || "not_found"] ?? "invite.reason.not_found")
    : "";

  const signedOutChoices = (
    <div className="space-y-2">
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {p?.email_bound ? t("invite.signInEmailBound") : t("invite.signInFirst")}
      </p>
      <Link href={`/login?next=${encodeURIComponent(back)}`} className="block">
        <Button className="w-full">
          <LogInIcon className="mr-1 h-4 w-4" /> {t("invite.signIn")}
        </Button>
      </Link>
      {passwordLogin && (
        <Link href={`/login?next=${encodeURIComponent(back)}&mode=signup`} className="block">
          <Button variant="outline" className="w-full">
            <UserPlusIcon className="mr-1 h-4 w-4" /> {t("invite.signUp")}
          </Button>
        </Link>
      )}
      {googleEnabled && (
        <Button
          type="button" variant="outline" className="w-full"
          onClick={() => void signIn("google", { callbackUrl: back })}
        >
          {t("login.continueWithGoogle")}
        </Button>
      )}
      {ssoName && (
        <div className="grid">
          <SsoButton name={ssoName} callbackUrl={back} primary={false} />
        </div>
      )}
      {(googleEnabled || ssoName) && p?.email_bound && (
        <p className="text-[11px] text-[var(--color-muted-foreground)]">
          {t("invite.verifiedJoinsAutomatically")}
        </p>
      )}
    </div>
  );

  return (
    <div className="flex min-h-screen items-center justify-center p-4 sm:p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BuildingIcon className="h-5 w-5" /> {t("invite.title")}
          </CardTitle>
          <CardDescription>
            {preview.isLoading ? t("common.loading") : p?.valid
              ? t("invite.grants", { workspace: p.workspace_name, role: roleLabel(t, p.role) })
              : t("invite.invalid")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {preview.isError && (
            <Callout tone="danger">{(preview.error as Error).message}</Callout>
          )}

          {p && !p.valid && (
            <>
              <div className="flex items-start gap-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
                <XCircleIcon className="mt-0.5 h-4 w-4 shrink-0" />
                {reasonText}
              </div>
              {/* A used email-bound link, opened again by the person it was for
                  (or by a Google/SSO newcomer who was joined at sign-in): the
                  API answers with the workspace, so offer to open it. */}
              {p.reason === "used" && status === "authenticated" && (
                <Button variant="outline" className="w-full" onClick={accept} disabled={busy}>
                  {t("invite.openJoined")}
                </Button>
              )}
              {status === "authenticated" && (
                <Link href="/access-request" className="block text-center text-xs underline">
                  {t("invite.askForAccessInstead")}
                </Link>
              )}
            </>
          )}

          {p?.valid && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant="outline">{roleLabel(t, p.role)}</Badge>
                {p.email_bound && (
                  <span className="text-[11px] text-[var(--color-muted-foreground)]">
                    {t("invite.emailBound")}
                  </span>
                )}
              </div>
              {p.invited_by ? (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("invite.invitedBy", { name: p.invited_by })}
                </p>
              ) : null}

              {status === "authenticated" ? (
                <div className="space-y-2">
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("invite.signedInAs", { email: session?.user?.email ?? "" })}
                  </p>
                  <Button className="w-full" onClick={accept} disabled={busy}>
                    <CheckIcon className="mr-1 h-4 w-4" />
                    {busy ? t("common.saving") : t("invite.accept")}
                  </Button>
                  {acceptError && (
                    <Callout tone="danger" className="space-y-2">
                      <p>{acceptError}</p>
                      {p.email_bound && (
                        <button
                          type="button"
                          className="underline"
                          onClick={() => {
                            forgetAgentSession();
                            void signOut({ callbackUrl: `/login?next=${encodeURIComponent(back)}` });
                          }}
                        >
                          {t("invite.switchAccount")}
                        </button>
                      )}
                    </Callout>
                  )}
                </div>
              ) : status === "loading" ? null : signedOutChoices}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
