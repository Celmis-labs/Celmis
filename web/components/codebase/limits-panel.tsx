"use client";

/**
 * The pack limits of this workspace, for its admins.
 *
 * Each field shows what is inherited (the install's or the built-in value) as its
 * placeholder; a number typed here overrides it for this workspace, an empty field
 * inherits. The server holds every value under a hard ceiling and refuses a number
 * above it, so the page only states the ceiling.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { codebaseApi, type PackLimitValues, type PackLimits } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePackLimits } from "@/lib/use-pack-limits";
import { useToken } from "@/lib/use-token";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Field = keyof PackLimitValues;
/** Where an inherited value comes from: the build, or the install's environment. */
const INHERITED_FROM = ["builtin", "install"] as const;
function inheritedFrom(value: number, builtin: number): (typeof INHERITED_FROM)[number] {
  return value === builtin ? INHERITED_FROM[0] : INHERITED_FROM[1];
}

const FIELDS: Field[] = [
  "archive_bytes", "max_files", "file_bytes", "total_bytes", "packs_per_workspace", "keep_versions",
];
const BYTES = new Set<Field>(["archive_bytes", "file_bytes", "total_bytes"]);
const KB = 1024;

/** Shown and typed in KB for the byte fields, as is for the counts. */
const shown = (f: Field, n: number) => {
  if (!BYTES.has(f)) return String(n);
  // a limit set in bytes (env) need not be whole KB: 1000 B is not "1"
  return n % KB === 0 ? String(n / KB) : String(Number((n / KB).toFixed(3)));
};
const sent = (f: Field, text: string): number | null => {
  const n = Number(text.trim());
  if (!text.trim() || !Number.isFinite(n) || n <= 0) return null;
  return BYTES.has(f) ? Math.round(n * KB) : Math.round(n);
};

export function LimitsPanel() {
  const { data } = usePackLimits();
  if (!data?.can_edit) return null;
  return <LimitsForm key={JSON.stringify(data.overrides)} data={data} />;
}

function LimitsForm({ data }: { data: PackLimits }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Record<Field, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => {
      const own = data.overrides[f];
      return [f, own === null || own === undefined ? "" : shown(f, own)];
    })) as Record<Field, string>);

  const save = useMutation({
    mutationFn: () => codebaseApi.setLimits(
      token!, Object.fromEntries(FIELDS.map((f) => [f, sent(f, draft[f])]))),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["codebase", "limits"] });
      toast.success(t("codebase.limits.saved"));
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("codebase.limits.title")}</CardTitle>
        <CardDescription>{t("codebase.limits.desc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {FIELDS.map((f) => {
            const from = inheritedFrom(data.defaults[f], data.builtin[f]);
            return (
            <div key={f} className="space-y-1">
              <Label htmlFor={`limit-${f}`}>{t(`codebase.limits.field.${f}`)}</Label>
              <Input
                id={`limit-${f}`} inputMode="numeric" value={draft[f]}
                onChange={(e) => setDraft({ ...draft, [f]: e.target.value.replace(/[^0-9]/g, "") })}
                placeholder={t("codebase.limits.inherited", {
                  value: shown(f, data.defaults[f]),
                  source: t(`codebase.limits.source.${from}`),
                })}
              />
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.limits.ceiling", { max: shown(f, data.ceilings[f]) })}
              </p>
            </div>
            );
          })}
        </div>
        {save.isError && <Callout tone="danger">{(save.error as Error).message}</Callout>}
        <div className="flex gap-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>{t("codebase.limits.save")}</Button>
          <Button
            variant="outline" disabled={save.isPending}
            onClick={() => setDraft(Object.fromEntries(FIELDS.map((f) => [f, ""])) as Record<Field, string>)}
          >
            {t("codebase.limits.clear")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
