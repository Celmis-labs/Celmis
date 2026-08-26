"use client";

/**
 * /admin/compliance — first-class compliance rules that hard-block APPROVE.
 * One row per rule. Blocking + severity=error means any failing check on a
 * matching PR forces a REQUEST_CHANGES verdict regardless of other findings.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  PlusIcon, ShieldAlertIcon, Trash2Icon, SaveIcon,
} from "lucide-react";

import {
  complianceApi, type ComplianceCheck, type ComplianceCheckIn,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

const EMPTY: ComplianceCheckIn = {
  name: "",
  description: "",
  scope: "workspace",
  glob_pattern: "**",
  rule: "",
  severity: "error",
  blocking: true,
  enabled: true,
};

export default function CompliancePage() {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const [creating, setCreating] = useState(false);
  const list = useQuery({
    queryKey: ["compliance"],
    queryFn: () => complianceApi.list(token!),
    enabled: !!token,
  });

  const rows = list.data ?? [];
  const blocking = rows.filter((r) => r.blocking && r.enabled).length;

  return (
    <PageShell width="wide">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
          <ShieldAlertIcon className="h-6 w-6" /> {t("admin.compliance.title")}
        </h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          {t("admin.compliance.descriptionBefore")}
          <code>REQUEST_CHANGES</code>{" "}
          {t("admin.compliance.descriptionAfter")}
        </p>
        <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
          {t("admin.compliance.stats", {
            total: rows.length,
            blocking,
            disabled: rows.filter((r) => !r.enabled).length,
          })}
        </p>
      </div>

      <SectionTabs set="review" />

      <div className="flex justify-end">
        <Button onClick={() => setCreating(true)} disabled={creating}>
          <PlusIcon className="h-4 w-4 mr-1" /> {t("admin.compliance.newCheck")}
        </Button>
      </div>

      {creating && (
        <CheckEditor
          initial={EMPTY}
          onSave={async (payload) => {
            await complianceApi.create(token!, payload);
            setCreating(false);
            qc.invalidateQueries({ queryKey: ["compliance"] });
            toast.success(t("admin.compliance.created"));
          }}
          onCancel={() => setCreating(false)}
        />
      )}

      {list.isLoading && (
        <div className="text-sm text-[var(--color-muted-foreground)]">{t("admin.compliance.loading")}</div>
      )}

      {rows.length === 0 && !list.isLoading && !creating && (
        <div className="text-sm text-[var(--color-muted-foreground)]">
          {t("admin.compliance.empty")}
        </div>
      )}

      {rows.map((row) => (
        <ExistingCheck
          key={row.id}
          row={row}
          onSave={async (payload) => {
            await complianceApi.update(token!, row.id, payload);
            qc.invalidateQueries({ queryKey: ["compliance"] });
            toast.success(t("admin.compliance.saved"));
          }}
          onRemove={async () => {
            const ok = await confirm({
              title: t("admin.compliance.confirmDelete", { name: row.name }),
              confirmLabel: t("common.delete"),
              danger: true,
            });
            if (!ok) return;
            await complianceApi.remove(token!, row.id);
            qc.invalidateQueries({ queryKey: ["compliance"] });
            toast.success(t("admin.compliance.removed"));
          }}
        />
      ))}
      {dialog}
    </PageShell>
  );
}


function ExistingCheck({
  row, onSave, onRemove,
}: {
  row: ComplianceCheck;
  onSave: (p: ComplianceCheckIn) => Promise<void>;
  onRemove: () => Promise<void>;
}) {
  const t = useT();

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex justify-between items-start gap-3">
          <div className="min-w-0">
            <CardTitle className="flex flex-wrap items-center gap-2">
              {row.name}
              {row.blocking && <Badge variant="brand">{t("admin.compliance.badgeBlocking")}</Badge>}
              {!row.enabled && <Badge variant="outline">{t("admin.compliance.badgeDisabled")}</Badge>}
              <Badge variant="outline" className="font-mono text-[10px]">{row.scope}</Badge>
              <Badge variant="outline" className="font-mono text-[10px]">{row.glob_pattern}</Badge>
            </CardTitle>
            {row.description && (
              <CardDescription className="mt-1">{row.description}</CardDescription>
            )}
          </div>
          <Button variant="ghost" size="icon" onClick={onRemove} aria-label={t("admin.compliance.deleteAria")}>
            <Trash2Icon className="h-4 w-4 text-red-600" />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <CheckEditor initial={row} onSave={onSave} />
      </CardContent>
    </Card>
  );
}


function CheckEditor({
  initial, onSave, onCancel,
}: {
  initial: ComplianceCheckIn;
  onSave: (p: ComplianceCheckIn) => Promise<void>;
  onCancel?: () => void;
}) {
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [scope, setScope] = useState(initial.scope);
  const [glob, setGlob] = useState(initial.glob_pattern);
  const [rule, setRule] = useState(initial.rule);
  const [severity, setSeverity] = useState<"error" | "warn">(initial.severity);
  const [blocking, setBlocking] = useState(initial.blocking);
  const [enabled, setEnabled] = useState(initial.enabled);
  const t = useT();

  const save = useMutation({
    mutationFn: () => onSave({
      name, description, scope, glob_pattern: glob, rule, severity,
      blocking, enabled,
    }),
    onError: (e) => toast.error(t("admin.compliance.saveFailed", { message: (e as Error).message })),
  });

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>{t("admin.compliance.labelName")}</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label>{t("admin.compliance.labelScope")}</Label>
          <Input value={scope} onChange={(e) => setScope(e.target.value)}
                 placeholder={t("admin.compliance.placeholderScope")} />
        </div>
        <div>
          <Label>{t("admin.compliance.labelGlob")}</Label>
          <Input value={glob} onChange={(e) => setGlob(e.target.value)}
                 placeholder={t("admin.compliance.placeholderGlob")} />
        </div>
        <div>
          <Label>{t("admin.compliance.labelSeverity")}</Label>
          <Select
            className="w-full"
            value={severity}
            onChange={(v) => setSeverity(v as "error" | "warn")}
            options={[
              { value: "error", label: t("admin.compliance.severityError") },
              { value: "warn", label: t("admin.compliance.severityWarn") },
            ]}
          />
        </div>
      </div>
      <div>
        <Label>{t("admin.compliance.labelRule")}</Label>
        <Textarea rows={3} value={rule} onChange={(e) => setRule(e.target.value)}
                  placeholder={t("admin.compliance.placeholderRule")} />
      </div>
      <div>
        <Label>{t("admin.compliance.labelDescription")}</Label>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="flex items-center gap-4 text-sm">
        <label className="flex items-center gap-2">
          <Switch checked={blocking} onCheckedChange={setBlocking} />
          {t("admin.compliance.blockingToggle")}
        </label>
        <label className="flex items-center gap-2">
          <Switch checked={enabled} onCheckedChange={setEnabled} />
          {t("admin.compliance.enabledToggle")}
        </label>
      </div>
      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button variant="ghost" onClick={onCancel}>{t("admin.compliance.cancel")}</Button>
        )}
        <Button onClick={() => save.mutate()} disabled={save.isPending || !name.trim() || !rule.trim()}>
          <SaveIcon className="h-4 w-4 mr-1" />
          {save.isPending ? t("admin.compliance.saving") : t("admin.compliance.save")}
        </Button>
      </div>
    </div>
  );
}
