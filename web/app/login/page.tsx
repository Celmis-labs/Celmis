/**
 * /login — server wrapper so the page knows which sign-in methods this
 * install allows. AUTH_PASSWORD_LOGIN is a runtime env var on the server; a
 * client component could only see it if it were baked into the bundle at
 * build time, which a published image cannot do.
 */
import { LoginForm } from "./login-form";
import { passwordLoginEnabled } from "@/lib/auth-options";

// Read the env per request, not once at build.
export const dynamic = "force-dynamic";

export default function LoginPage() {
  return <LoginForm passwordLogin={passwordLoginEnabled()} />;
}
