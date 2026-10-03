/**
 * Workspace roles, lowest to highest. Mirrors `WORKSPACE_ROLE_RANK` in
 * src/users/roles.py (viewer 1 … owner 5), and so does the grant rule below.
 *
 * editor is the prompt editor: agent prompts, review policies and analytics
 * in its workspace — no members, invites, keys or connections.
 *
 * Who may grant what: owner/admin/editor only the superadmin (the env master
 * account); member/viewer also the workspace's own owner/admin. The API
 * enforces it (`can_change`); this copy only decides which controls to draw.
 */
export const WS_ROLES = ["viewer", "member", "editor", "admin", "owner"] as const;
export type WorkspaceRole = (typeof WS_ROLES)[number];

/** Roles only the superadmin may grant, change to or from, or remove. */
export const PRIVILEGED_ROLES: readonly string[] = ["owner", "admin", "editor"];
/** Roles a workspace owner/admin hands out themselves. */
export const DELEGABLE_ROLES: readonly string[] = ["member", "viewer"];
/** Roles that administer a workspace. */
export const WORKSPACE_ADMIN_ROLES: readonly string[] = ["owner", "admin"];
/** Roles that may edit agent prompts and review policies. */
export const PROMPT_EDITOR_ROLES: readonly string[] = ["owner", "admin", "editor"];

/** Team labels — `TEAM_ROLES` in src/users/roles.py: the workspace ladder
 *  plus the team-only `reviewer`, highest first. */
export const TEAM_ROLES = ["owner", "admin", "editor", "reviewer", "member", "viewer"] as const;

/** Localised label for a role; an unknown role is shown as-is. */
export function roleLabel(t: (key: string) => string, role: string): string {
  return (WS_ROLES as readonly string[]).includes(role) ? t(`roles.${role}`) : role;
}

/** Roles this actor may grant in a workspace where they hold `actorRole`. */
export function grantableRoles(isSuperadmin: boolean, actorRole: string | null | undefined): string[] {
  if (isSuperadmin) return [...WS_ROLES];
  if (actorRole && WORKSPACE_ADMIN_ROLES.includes(actorRole)) return [...DELEGABLE_ROLES];
  return [];
}

/** May this actor move a member from `current` to `next` (null = add / remove)? */
export function canChangeMember(
  isSuperadmin: boolean,
  actorRole: string | null | undefined,
  current: string | null,
  next: string | null,
): boolean {
  if (isSuperadmin) return true;
  const allowed = grantableRoles(false, actorRole);
  if (allowed.length === 0) return false;
  if (current !== null && !allowed.includes(current)) return false;
  return next === null || allowed.includes(next);
}

/** Select options, highest role first (the order the pickers always used).
 *  Pass `only` to offer just the roles the actor may grant. */
export function roleOptions(
  t: (key: string) => string,
  only?: readonly string[],
): { value: string; label: string }[] {
  return [...WS_ROLES]
    .reverse()
    .filter((r) => !only || only.includes(r))
    .map((r) => ({ value: r, label: roleLabel(t, r) }));
}
