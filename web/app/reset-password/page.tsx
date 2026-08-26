"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { authApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PasswordStrength, passwordProblemKeys } from "@/components/password-strength";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function ResetForm() {
  const t = useT();
  const router = useRouter();
  const params = useSearchParams();
  const [token, setToken] = useState(params.get("token") ?? "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);

  const problems = passwordProblemKeys(password);
  const mismatch = confirm.length > 0 && confirm !== password;
  const canSubmit = token.trim() && password && problems.length === 0 && !mismatch;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await authApi.resetPassword(token.trim(), password);
      toast.success(t("auth.resetDone"));
      router.push("/login");
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
          <CardTitle>{t("auth.resetTitle")}</CardTitle>
          <CardDescription>{t("auth.resetDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form method="post" onSubmit={submit} className="space-y-3">
            {!params.get("token") && (
              <div>
                <Label htmlFor="token">{t("auth.resetToken")}</Label>
                <Input id="token" value={token} onChange={(e) => setToken(e.target.value)} required />
              </div>
            )}
            <div>
              <Label htmlFor="pw">{t("auth.newPassword")}</Label>
              <Input id="pw" type="password" autoComplete="new-password" required
                     value={password} onChange={(e) => setPassword(e.target.value)} />
              <PasswordStrength value={password} />
            </div>
            <div>
              <Label htmlFor="pw2">{t("auth.confirmPassword")}</Label>
              <Input id="pw2" type="password" autoComplete="new-password" required
                     value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              {mismatch && (
                <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                  {t("auth.passwordsDiffer")}
                </p>
              )}
            </div>
            <Button type="submit" className="w-full" disabled={busy || !canSubmit}>
              {busy ? t("common.saving") : t("auth.setNewPassword")}
            </Button>
          </form>
          <Link href="/login" className="mt-3 block text-center text-xs text-[var(--color-muted-foreground)] underline">
            {t("auth.backToLogin")}
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetForm />
    </Suspense>
  );
}
