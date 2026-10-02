/**
 * Workspace roles, lowest to highest. Mirrors `_ROLE_RANK` in
 * src/api/routers/workspaces.py (viewer 1 … owner 5).
 *
 * editor sits between member and admin: it can see workspace analytics but
 * gets no workspace-admin powers (members, invites, settings stay
 * owner/admin).
 */
export const WS_ROLES = ["viewer", "member", "editor", "admin", "owner"] as const;
export type WorkspaceRole = (typeof WS_ROLES)[number];

/** Localised label for a role; an unknown role is shown as-is. */
export function roleLabel(t: (key: string) => string, role: string): string {
  return (WS_ROLES as readonly string[]).includes(role) ? t(`roles.${role}`) : role;
}

/** Select options, highest role first (the order the pickers always used). */
export function roleOptions(t: (key: string) => string): { value: string; label: string }[] {
  return [...WS_ROLES].reverse().map((r) => ({ value: r, label: roleLabel(t, r) }));
}
