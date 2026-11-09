/**
 * /oauth/consent — where an MCP client sends the browser to ask the person
 * whether it may connect (see src/api/routers/oauth.py).
 *
 * It sits behind the normal sign-in guard (proxy.ts): a signed-out visitor goes
 * through /login, SSO included, and comes back here with the query intact.
 */
import { Suspense } from "react";

import { ConsentView } from "./consent-view";

export const dynamic = "force-dynamic";

export default function OAuthConsentPage() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <Suspense fallback={null}>
        <ConsentView />
      </Suspense>
    </main>
  );
}
