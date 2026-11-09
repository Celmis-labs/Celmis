"use client";

import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { OAuthConsentCard } from "@/components/oauth-consent-card";
import { Callout } from "@/components/ui/callout";
import { useT } from "@/lib/i18n";
import {
  fetchConsentInfo, isSafeRedirect, parseConsentRequest, submitConsent,
} from "@/lib/oauth-consent";
import { useToken } from "@/lib/use-token";

export function ConsentView() {
  const t = useT();
  const token = useToken();
  const params = useSearchParams();
  const request = parseConsentRequest(params);
  // Empty until the person picks one: the API then uses the active workspace.
  const [picked, setPicked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);

  const info = useQuery({
    queryKey: ["oauth-consent-info", params.toString(), picked],
    queryFn: () => fetchConsentInfo(token!, request!, picked),
    enabled: !!token && !!request,
    retry: false,
  });

  if (!request) {
    return <Callout tone="danger" className="max-w-lg">{t("oauth.consent.badRequest")}</Callout>;
  }
  if (info.isError) {
    return <Callout tone="danger" className="max-w-lg">{(info.error as Error).message}</Callout>;
  }
  if (leaving) {
    return <Callout tone="info" className="max-w-lg">{t("oauth.consent.done")}</Callout>;
  }
  if (!info.data) {
    return <div className="text-sm text-[var(--color-muted-foreground)]">{t("oauth.consent.loading")}</div>;
  }

  const decide = async (decision: "allow" | "deny") => {
    setBusy(true);
    setError(null);
    try {
      const target = await submitConsent(token!, request, decision, picked ?? info.data!.workspace.slug);
      if (!isSafeRedirect(target, request.redirect_uri)) {
        setError(t("oauth.consent.badRedirect"));
        setBusy(false);
        return;
      }
      setLeaving(true);
      window.location.assign(target);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <OAuthConsentCard
      info={info.data}
      workspace={picked ?? info.data.workspace.slug}
      onWorkspace={setPicked}
      busy={busy}
      error={error}
      onAllow={() => decide("allow")}
      onDeny={() => decide("deny")}
    />
  );
}
