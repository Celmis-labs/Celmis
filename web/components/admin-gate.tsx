"use client";

import { useSession } from "next-auth/react";

import { Callout } from "@/components/ui/callout";
import { useT } from "@/lib/i18n";

/**
 * Client-side gate for global-admin-only pages.
 *
 * Shows a warning Callout instead of the page content when the signed-in
 * user is not a global admin. While the session is still loading the
 * children render as usual — the API enforces the real authorization, this
 * gate only replaces confusing 403 noise with an explanation.
 */
export function AdminGate({ children }: { children: React.ReactNode }) {
  const { data: session } = useSession();
  const t = useT();

  if (session && !session.isAdmin) {
    return (
      <div className="mx-auto w-full max-w-4xl p-8">
        <Callout tone="warning">{t("common.adminOnly")}</Callout>
      </div>
    );
  }
  return <>{children}</>;
}
