"use client";

/**
 * The teams a pack is granted to, each with what that team may do with it.
 * Controlled: the parent holds the list and decides when to save it (the
 * create dialog sends it with the pack, the Sharing tab with a Save button).
 */

import Link from "next/link";
import { XIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { PACK_PERMISSIONS } from "@/lib/codebase";
import type { PackPermission, Team } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";

export type TeamGrant = { team_id: string; permission: PackPermission };

export function TeamGrantsEditor({
  value,
  onChange,
  teams,
  names = {},
  disabled,
}: {
  value: TeamGrant[];
  onChange: (next: TeamGrant[]) => void;
  /** The workspace's teams (GET /api/teams). */
  teams: Team[];
  /** Names for granted teams missing from `teams` (so a row never shows an id). */
  names?: Record<string, string>;
  disabled?: boolean;
}) {
  const t = useT();
  const nameOf = (id: string) => teams.find((x) => x.id === id)?.name ?? names[id] ?? id;
  const free = teams.filter((x) => !value.some((g) => g.team_id === x.id));
  const permissionOptions = PACK_PERMISSIONS.map((p) => ({
    value: p, label: t(`codebase.permission.${p}`),
  }));
  return (
    <div className="space-y-2">
      {value.length === 0 && (
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.teams.none")}</p>
      )}
      {value.map((grant) => (
        <div
          key={grant.team_id}
          className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2"
        >
          <span className="min-w-0 flex-1 truncate text-sm font-medium">{nameOf(grant.team_id)}</span>
          <Select
            value={grant.permission}
            disabled={disabled}
            className="w-36"
            options={permissionOptions}
            onChange={(p) =>
              onChange(value.map((g) =>
                g.team_id === grant.team_id ? { ...g, permission: p as PackPermission } : g))
            }
          />
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            disabled={disabled}
            aria-label={t("codebase.teams.remove", { name: nameOf(grant.team_id) })}
            onClick={() => onChange(value.filter((g) => g.team_id !== grant.team_id))}
          >
            <XIcon />
          </Button>
        </div>
      ))}
      {teams.length === 0 ? (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("codebase.teams.noTeams")}{" "}
          <Link href="/admin/teams" className="underline">{t("codebase.teams.manage")}</Link>
        </p>
      ) : free.length > 0 ? (
        <Select
          value=""
          disabled={disabled}
          placeholder={t("codebase.teams.add")}
          className="w-full sm:w-72"
          options={free.map((x) => ({ value: x.id, label: x.name }))}
          onChange={(id) => onChange([...value, { team_id: id, permission: "read" }])}
        />
      ) : null}
    </div>
  );
}
