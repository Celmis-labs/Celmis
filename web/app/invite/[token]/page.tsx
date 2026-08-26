"use client";

/**
 * /invite/{token} — invite landing page.
 *
 * Works for both signed-in and signed-out visitors: the preview is public so
 * the page can say what the link grants before asking anyone to log in.
 */

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { BuildingIcon, CheckIcon, XCircleIcon } from "lucide-react";

import { invitesApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token: inviteToken } = use(params);
  const t = useT();
  const router = useRouter();
  const jwt = useToken();
  const { status } = useSession();
  const [busy, setBusy] = useState(false);

  const preview = useQuery({
    queryKey: ["invite", inviteToken],
    queryFn: () => invitesApi.preview(inviteToken),
  });

  const accept = async () => {
    if (!jwt) return;
    setBusy(true);
    try {
      const r = await invitesApi.accept(jwt, inviteToken);
      // Switch the active workspace to the one we just joined.
      document.cookie = `x-workspace=${r.workspace_slug}; path=/; max-age=31536000; SameSite=Lax`;
      toast.success(t("invite.joined", { workspace: r.workspace_slug, role: r.role }));
      router.push("/dashboard");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const p = preview.data;

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BuildingIcon className="h-5 w-5" /> {t("invite.title")}
          </CardTitle>
          <CardDescription>
            {preview.isLoading ? t("common.loading") : p?.valid
              ? t("invite.grants", { workspace: p.workspace_name, role: p.role })
              : t("invite.invalid")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {p && !p.valid && (
            <div className="flex items-start gap-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400">
              <XCircleIcon className="mt-0.5 h-4 w-4 shrink-0" />
              {p.detail}
            </div>
          )}

          {p?.valid && (
            <>
              <div className="flex items-center gap-2 text-sm">
                <Badge variant="outline">{p.role}</Badge>
                {p.email_bound && (
                  <span className="text-[11px] text-[var(--color-muted-foreground)]">
                    {t("invite.emailBound")}
                  </span>
                )}
              </div>

              {status === "authenticated" ? (
                <Button className="w-full" onClick={accept} disabled={busy}>
                  <CheckIcon className="h-4 w-4 mr-1" />
                  {busy ? t("common.saving") : t("invite.accept")}
                </Button>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("invite.signInFirst")}
                  </p>
                  <Link href={`/login?next=/invite/${inviteToken}`}>
                    <Button className="w-full">{t("invite.signIn")}</Button>
                  </Link>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
