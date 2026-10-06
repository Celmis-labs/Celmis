"use client";

/** Commands: what people can tell the reviewer in a pull-request comment
 *  (`@celmis start-review`, `@celmis review --force`, `@celmis help`), and who
 *  may. The handle itself is an install setting (REVIEW_BOT_HANDLE). */

import { useT } from "@/lib/i18n";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, ChoiceRow, Group, SectionFrame,
} from "@/components/review-settings/field";
import { effective } from "@/components/review-settings/model";

/** The order a reader compares them in; the server's list decides which
 *  exist (`setting_choices`), this only sorts. */
const PERMISSION_ORDER = ["repo_access", "participants", "anyone"];

export function CommandsSection() {
  const t = useT();
  const { scope, draft, inh, meta } = useSettings();
  const on = Boolean(effective(draft, "commands_enabled", scope.kind, inh));
  const served = meta.choices.command_permission ?? PERMISSION_ORDER;
  const options = [
    ...PERMISSION_ORDER.filter((v) => served.includes(v)),
    ...served.filter((v) => !PERMISSION_ORDER.includes(v)),
  ].map((v) => ({
    value: v,
    title: t(`reviewSettings.commands.permission.${v}`),
    body: t(`reviewSettings.commands.permission.${v}Body`),
  }));
  return (
    <SectionFrame
      id="commands"
      title={t("reviewSettings.section.commands")}
      description={t("reviewSettings.commands.desc")}
    >
      <Group>
        <BooleanRow
          field="commands_enabled"
          label={t("reviewSettings.commands.enabled")}
          description={t("reviewSettings.commands.enabledHint")}
        />
        <BooleanRow
          field="chat_enabled"
          label={t("reviewSettings.commands.chat")}
          description={t("reviewSettings.commands.chatHint")}
          disabled={!on}
        />
        <ChoiceRow
          field="command_permission"
          label={t("reviewSettings.commands.permission")}
          description={t("reviewSettings.commands.permissionHint")}
          options={options}
          columns={3}
          disabled={!on}
        />
      </Group>
    </SectionFrame>
  );
}
