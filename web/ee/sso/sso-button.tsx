"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * "Sign in with <name>" — the company SSO button on /login.
 *
 * The login page renders it only when the web side is configured for OIDC
 * AND the API reports the `sso` capability (see app/login/page.tsx), so this
 * component does not check either again.
 */
import { signIn } from "next-auth/react";
import { ShieldCheckIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useT } from "@/lib/i18n";

export function SsoButton({
  name,
  callbackUrl,
  primary,
}: {
  /** The provider's display name (AUTH_OIDC_NAME, default "SSO"). */
  name: string;
  callbackUrl: string;
  /** The main way in (password sign-in is off) rather than an alternative. */
  primary: boolean;
}) {
  const t = useT();
  return (
    <Button
      type="button"
      onClick={() => void signIn("oidc", { callbackUrl })}
      variant={primary ? "default" : "outline"}
    >
      <ShieldCheckIcon className="h-4 w-4" aria-hidden />
      {t("auth.sso.signInWith", { name })}
    </Button>
  );
}
