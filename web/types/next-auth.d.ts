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
    isAdmin?: boolean;
    isSuperadmin?: boolean;
    userId?: string;
    /** ms epoch of the last /api/auth/me read (keeps isAdmin fresh). */
    meCheckedAt?: number;
  }
}
