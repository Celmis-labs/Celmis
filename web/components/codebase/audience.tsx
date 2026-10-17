"use client";

/**
 * Who a pack is shown to, said the same way everywhere it is chosen or read:
 * the badge on a card, the picker in the create dialog and on the Sharing tab,
 * and the table that answers "who actually sees this?".
 *
 * The rules are the API's (`src/knowledge/visibility.py`); this restates them
 * for the person choosing. The restricted level is the one worth a warning:
 * workspace owners and admins do NOT see it, which is the point of it and also
 * the surprise, so the picker says so in the place the choice is made.
 */

import { CheckIcon, GlobeIcon, LockIcon, MinusIcon, UsersIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { audienceKey, isPrivatePack, PACK_AUDIENCES } from "@/lib/codebase";
import type { PackAudience } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { Select } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const AUDIENCE_LOOK: Record<PackAudience, { icon: typeof LockIcon; variant: BadgeVariant }> = {
  workspace: { icon: GlobeIcon, variant: "info" },
  teams: { icon: UsersIcon, variant: "brand" },
  restricted: { icon: LockIcon, variant: "warning" },
};

/** `teamCount` is the number of teams the pack is granted to, known only to
 *  its maintainers; with 0 a restricted pack is named "private". */
export function AudienceBadge({ audience, teamCount }: { audience: PackAudience; teamCount?: number }) {
  const t = useT();
  const look = AUDIENCE_LOOK[audience] ?? AUDIENCE_LOOK.teams;
  const Icon = look.icon;
  return (
    <Badge variant={look.variant}>
      <Icon aria-hidden />
      {t(audienceKey(audience, teamCount))}
    </Badge>
  );
}

export function AudiencePicker({
  value,
  onChange,
  disabled,
  id,
  teamCount,
}: {
  value: PackAudience;
  onChange: (next: PackAudience) => void;
  disabled?: boolean;
  id?: string;
  /** Teams the pack will be granted to: with none, "restricted" is private. */
  teamCount?: number;
}) {
  const t = useT();
  // "Restricted" with no team behind it is the private pack; its words differ
  // in all four places the level is spoken (name, hint, description, warning).
  const isPrivate = isPrivatePack(value, teamCount);
  const hintOf = (a: PackAudience) =>
    isPrivatePack(a, teamCount) ? t("codebase.audience.private.short") : t(`codebase.audience.${a}.short`);
  const descOf = (a: PackAudience) =>
    isPrivatePack(a, teamCount) ? t("codebase.audience.private.desc") : t(`codebase.audience.${a}.desc`);
  return (
    <div className="space-y-2">
      <Select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(v) => onChange(v as PackAudience)}
        className="w-full sm:w-72"
        options={PACK_AUDIENCES.map((a) => ({
          value: a,
          label: t(audienceKey(a, teamCount)),
          hint: hintOf(a),
        }))}
      />
      <p className="text-xs leading-relaxed text-[var(--color-muted-foreground)]">
        {descOf(value)}
      </p>
      {value === "restricted" && (
        <Callout tone="warning">
          {t(isPrivate ? "codebase.audience.privateWarning" : "codebase.audience.restrictedWarning")}
        </Callout>
      )}
    </div>
  );
}

/** Audience → who sees it, one row per kind of person. */
const VISIBILITY: { who: string; sees: Record<PackAudience, boolean> }[] = [
  { who: "superadmin", sees: { workspace: true, teams: true, restricted: true } },
  { who: "globalAdmin", sees: { workspace: true, teams: true, restricted: false } },
  { who: "workspaceAdmin", sees: { workspace: true, teams: true, restricted: false } },
  { who: "grantedTeam", sees: { workspace: true, teams: true, restricted: true } },
  { who: "named", sees: { workspace: true, teams: true, restricted: true } },
  { who: "member", sees: { workspace: true, teams: false, restricted: false } },
  { who: "outsider", sees: { workspace: false, teams: false, restricted: false } },
];

/** The three levels side by side. `current` lights the column of the level the
 *  pack has now, so the table answers the question for THIS pack. */
export function AudienceGuide({ current }: { current?: PackAudience }) {
  const t = useT();
  return (
    <div className="space-y-2">
      <Table>
        <THead>
          <TR>
            <TH>{t("codebase.guide.who")}</TH>
            {PACK_AUDIENCES.map((a) => (
              <TH key={a} className={cn("text-center", a === current && "text-[var(--color-foreground)]")}>
                {t(`codebase.audience.${a}`)}
              </TH>
            ))}
          </TR>
        </THead>
        <TBody>
          {VISIBILITY.map((row) => (
            <TR key={row.who}>
              <TD>{t(`codebase.guide.${row.who}`)}</TD>
              {PACK_AUDIENCES.map((a) => (
                <TD
                  key={a}
                  className={cn("text-center", a === current && "bg-[var(--color-primary-soft)]/40")}
                >
                  {row.sees[a] ? (
                    <CheckIcon aria-hidden className="mx-auto size-4 text-[var(--color-success)]" />
                  ) : (
                    <MinusIcon aria-hidden className="mx-auto size-4 text-[var(--color-subtle-foreground)]" />
                  )}
                  <span className="sr-only">
                    {row.sees[a] ? t("codebase.guide.sees") : t("codebase.guide.hidden")}
                  </span>
                </TD>
              ))}
            </TR>
          ))}
        </TBody>
      </Table>
      <p className="text-xs leading-relaxed text-[var(--color-muted-foreground)]">
        {t("codebase.guide.footnote")}
      </p>
      <p className="text-xs leading-relaxed text-[var(--color-muted-foreground)]">
        {t("codebase.guide.privateNote")}
      </p>
      <p className="text-xs leading-relaxed text-[var(--color-muted-foreground)]">
        {t("codebase.guide.maintain")}
      </p>
    </div>
  );
}
