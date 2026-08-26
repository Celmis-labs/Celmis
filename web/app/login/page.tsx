"use client";
import { getProviders, signIn } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { toast } from "sonner";
import { BrandMark, BrandWord } from "@/components/brand-mark";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  PasswordStrength, passwordProblemKeys, PASSWORD_MIN_LENGTH,
} from "@/components/password-strength";
import { Label } from "@/components/ui/label";
import { LanguageSwitcher } from "@/components/language-switcher";
import { ThemeToggle } from "@/components/theme-toggle";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useT } from "@/lib/i18n";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner() {
  const router = useRouter();
  const params = useSearchParams();
  const t = useT();
  // Post-auth destination. `next` is what the invite page sends
  // (/invite/{token}), `from` is the older param — accept both so an invited
  // newcomer lands back on the invite to redeem it after signup. Only allow
  // same-origin relative paths (no `//host` or absolute URLs) to avoid an
  // open redirect.
  const rawNext = params.get("next") || params.get("from") || "/dashboard";
  const callback =
    rawNext.startsWith("/") && !rawNext.startsWith("//") ? rawNext : "/dashboard";
  const [pending, startTransition] = useTransition();
  const [mode, setMode] = useState<"login" | "signup">("login");
  // Live strength feedback mirrors the server-side policy exactly.
  const [signupPassword, setSignupPassword] = useState("");
  const [signupEmail, setSignupEmail] = useState("");
  // Ask NextAuth which providers are actually configured. Showing a Google
  // button when GOOGLE_CLIENT_ID is unset sends users to a 400 from Google.
  const [googleEnabled, setGoogleEnabled] = useState(false);
  useEffect(() => {
    getProviders()
      .then((providers) => setGoogleEnabled(Boolean(providers && "google" in providers)))
      .catch(() => setGoogleEnabled(false));
  }, []);

  const onCredentials = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const email = String(form.get("email") || "").trim();
    const password = String(form.get("password") || "");
    const name = String(form.get("name") || "");
    if (!email || !password) return;
    startTransition(async () => {
      const res = await signIn("credentials", {
        email,
        password,
        name,
        mode,
        redirect: false,
      });
      if (res?.error) {
        toast.error(
          mode === "signup" ? t("login.signupFailed") : t("login.invalidCredentials"),
        );
        return;
      }
      toast.success(mode === "signup" ? t("login.accountCreated") : t("login.welcomeToast"));
      router.push(callback);
      router.refresh();
    });
  };

  const onGoogle = () => {
    void signIn("google", { callbackUrl: callback });
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center bg-gradient-to-br from-[var(--color-brand-muted)] to-[var(--color-background)] px-4">
      <div className="absolute right-4 top-4 flex items-center gap-2">
        <LanguageSwitcher direction="down" />
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md">
        <div className="flex items-center gap-3 mb-8">
          <BrandMark size="lg" />
          <div>
            <BrandWord className="text-2xl" />
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("login.tagline")}
            </p>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-xl">
              {mode === "signup" ? t("login.createTitle") : t("login.signIn")}
            </CardTitle>
            <CardDescription>
              {mode === "signup"
                ? t("login.createSubtitle")
                : googleEnabled
                  ? t("login.welcomeGoogle")
                  : t("login.welcome")}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Tabs value={mode} onValueChange={(v) => setMode(v as "login" | "signup")}>
              <TabsList className="w-full grid grid-cols-2">
                <TabsTrigger value="login">{t("login.signIn")}</TabsTrigger>
                <TabsTrigger value="signup">{t("login.signUp")}</TabsTrigger>
              </TabsList>

              <TabsContent value="login">
                <form method="post" onSubmit={onCredentials} className="flex flex-col gap-3">
                  <div className="grid gap-2">
                    <Label htmlFor="email">{t("login.email")}</Label>
                    <Input id="email" name="email" type="email" autoComplete="email" required />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="password">{t("login.password")}</Label>
                    <Input
                      id="password"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      required
                    />
                    <Link
                      href="/forgot-password"
                      className="-my-2 inline-flex min-h-11 items-center self-end text-xs text-[var(--color-muted-foreground)] underline hover:text-[var(--color-foreground)] sm:my-0 sm:min-h-0"
                    >
                      {t("login.forgotPassword")}
                    </Link>
                  </div>
                  <Button type="submit" disabled={pending} className="mt-2">
                    {pending ? t("login.signingIn") : t("login.signIn")}
                  </Button>
                </form>
              </TabsContent>

              <TabsContent value="signup">
                <form method="post" onSubmit={onCredentials} className="flex flex-col gap-3">
                  <div className="grid gap-2">
                    <Label htmlFor="name">{t("login.name")}</Label>
                    <Input id="name" name="name" type="text" autoComplete="name" />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="email-su">{t("login.email")}</Label>
                    <Input id="email-su" name="email" type="email" autoComplete="email" required
                           value={signupEmail} onChange={(e) => setSignupEmail(e.target.value)} />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="password-su">{t("login.password")}</Label>
                    <Input
                      id="password-su"
                      name="password"
                      type="password"
                      autoComplete="new-password"
                      minLength={PASSWORD_MIN_LENGTH}
                      required
                      value={signupPassword}
                      onChange={(e) => setSignupPassword(e.target.value)}
                    />
                    <PasswordStrength value={signupPassword} email={signupEmail} />
                  </div>
                  <Button
                    type="submit"
                    disabled={pending || passwordProblemKeys(signupPassword, signupEmail).length > 0}
                    className="mt-2"
                  >
                    {pending ? t("login.creatingAccount") : t("login.createAccount")}
                  </Button>
                </form>
              </TabsContent>
            </Tabs>

            {googleEnabled && (
              <>
            <div className="relative my-2">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-[var(--color-card)] px-2 text-[var(--color-muted-foreground)]">
                  {t("login.or")}
                </span>
              </div>
            </div>

            <Button type="button" variant="outline" onClick={onGoogle}>
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 48 48"
                className="h-4 w-4"
                aria-hidden
              >
                <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32 29.3 35 24 35c-6.6 0-12-5.4-12-12s5.4-12 12-12c3 0 5.7 1.1 7.8 3l5.7-5.7C34 4.7 29.3 3 24 3 12.4 3 3 12.4 3 24s9.4 21 21 21 21-9.4 21-21c0-1.4-.1-2.4-.4-3.5z"/>
                <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 16 19 13 24 13c3 0 5.7 1.1 7.8 3l5.7-5.7C34 7.7 29.3 6 24 6c-7 0-13 4-16.7 8.7z"/>
                <path fill="#4CAF50" d="M24 45c5.2 0 10-2 13.6-5.2l-6.3-5.3C29.3 36 26.7 37 24 37c-5.3 0-9.7-3-11.3-7.5l-6.5 5C9.6 41 16.3 45 24 45z"/>
                <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.7 2-2 3.7-3.7 4.9l6.3 5.3C40.6 35.3 45 30 45 24c0-1.4-.1-2.4-1.4-3.5z"/>
              </svg>
              {t("login.continueWithGoogle")}
            </Button>
              </>
            )}
          </CardContent>
        </Card>

        <p className="mt-6 text-center text-xs text-[var(--color-muted-foreground)]">
          {t("login.tokensNote")}
        </p>
      </div>
    </div>
  );
}
