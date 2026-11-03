import "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    celmisToken: string | null;
    celmisExpiresAt: string | null;
    isAdmin: boolean;
    /** The env master account — grants owner/admin/editor, creates shared
     *  workspaces, opens /admin/users. Not every global admin is one. */
    isSuperadmin: boolean;
    user: {
      id: string;
      email: string;
      name?: string | null;
      image?: string | null;
    };
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    celmisToken?: string;
    celmisExpiresAt?: string;
    /** Renews the API token. Lives only in the encrypted session cookie: it is
     *  never copied into the Session object that the browser can read. */
    refreshToken?: string;
    /** The OIDC provider's id_token, kept for federated sign-out
     *  (id_token_hint). Same rule: cookie only, never in the Session. */
    idToken?: string;
    isAdmin?: boolean;
    isSuperadmin?: boolean;
    userId?: string;
    /** ms epoch of the last /api/auth/me read (keeps isAdmin fresh). */
    meCheckedAt?: number;
  }
}
