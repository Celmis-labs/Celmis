/**
 * /invite/{token} — server wrapper for the invite landing page.
 *
 * Like /login, it has to know which sign-in methods this install allows
 * before it draws the buttons a signed-out invitee chooses from: password
 * sign-up only when AUTH_PASSWORD_LOGIN allows it, company SSO only when the
 * web side is configured AND the API is licensed for it. Those are runtime
 * env on the server, so the decision is made here and handed to the client.
 */
import { InviteView } from "./invite-view";
import { oidcConfigured, oidcProviderName, passwordLoginEnabled } from "@/lib/auth-options";
import { apiOffersSso } from "@/lib/sso-offer";

// Read the env per request, not once at build.
export const dynamic = "force-dynamic";

export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  const ssoName = oidcConfigured() && (await apiOffersSso()) ? oidcProviderName() : null;
  return <InviteView inviteToken={token} passwordLogin={passwordLoginEnabled()} ssoName={ssoName} />;
}
