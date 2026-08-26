"use client";

import { useState } from "react";
import Link from "next/link";
import { toast } from "sonner";

import { authApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function ForgotPasswordPage() {
  const t = useT();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await authApi.forgotPassword(email.trim());
      setSent(true);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>{t("auth.forgotTitle")}</CardTitle>
          <CardDescription>{t("auth.forgotDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {!sent ? (
            <form method="post" onSubmit={submit} className="space-y-3">
              <div>
                <Label htmlFor="email">{t("auth.email")}</Label>
                <Input id="email" type="email" required autoFocus value={email}
                       onChange={(e) => setEmail(e.target.value)} />
              </div>
              <Button type="submit" className="w-full" disabled={busy || !email.trim()}>
                {busy ? t("common.saving") : t("auth.sendResetLink")}
              </Button>
            </form>
          ) : (
            <div className="space-y-3">
              <p className="text-sm">{t("auth.forgotSent")}</p>
              <p className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/50 p-3 text-[11px] text-[var(--color-muted-foreground)]">
                {t("auth.noMailerNotice")}
              </p>
            </div>
          )}
          <Link href="/login" className="block text-center text-xs text-[var(--color-muted-foreground)] underline">
            {t("auth.backToLogin")}
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
