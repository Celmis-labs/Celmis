"use client";

/**
 * Narrow a share: hide more paths, end it sooner, turn questions off, lower the
 * number of people. Free in every build — a share only ever gets smaller here
 * (`PATCH /api/sharing/shares/{id}`), and anything wider is the licensed extend.
 *
 * The form can only build a body that narrows (`narrowBody`); the server refuses
 * the rest again with a 409, so a stale page cannot widen a share either.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { ApiError, sharingApi, type ShareDetail } from "@/lib/api";
import {
  EMPTY_NARROW, MAX_DENY_GLOBS, narrowBody, narrowIntroKey, narrowProblem, refusalKey,
  type NarrowForm,
} from "@/lib/sharing";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export function NarrowDialog({
  share, canGrant, open, onOpenChange, onDone,
}: {
  share: ShareDetail;
  /** The licensed routes are mounted: the intro points at Extend instead of at the licence. */
  canGrant: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (share: ShareDetail) => void;
}) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("sharing.narrowDialog.title")}</DialogTitle>
          <DialogDescription>{t(narrowIntroKey(canGrant))}</DialogDescription>
        </DialogHeader>
        {/* Inside the content so that the form is built afresh each time it opens. */}
        <NarrowShareForm share={share} onCancel={() => onOpenChange(false)}
                    onDone={(s) => { onOpenChange(false); onDone(s); }} />
      </DialogContent>
    </Dialog>
  );
}

function NarrowShareForm({
  share, onCancel, onDone,
}: { share: ShareDetail; onCancel: () => void; onDone: (share: ShareDetail) => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [form, setForm] = useState<NarrowForm>(EMPTY_NARROW);
  const set = (patch: Partial<NarrowForm>) => setForm((cur) => ({ ...cur, ...patch }));

  const problem = narrowProblem(form, share);
  const body = narrowBody(form, share);
  const narrow = useMutation({
    mutationFn: () => sharingApi.narrow(token!, share.share_id, body!),
    onSuccess: (updated) => {
      toast.success(t("sharing.narrowed"));
      qc.invalidateQueries({ queryKey: ["sharing-shares"] });
      onDone(updated);
    },
    onError: (e: Error) => {
      const key = e instanceof ApiError ? refusalKey(e.status, e.message) : null;
      toast.error(key ? t(key) : e.message);
    },
  });

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <Label htmlFor="narrow-deny">{t("sharing.narrowDialog.deny")}</Label>
        <Textarea id="narrow-deny" rows={3} className="font-mono text-xs" value={form.addDeny}
                  onChange={(e) => set({ addDeny: e.target.value })} spellCheck={false} />
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("sharing.dialog.denyHint")}</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="narrow-days">{t("sharing.narrowDialog.days")}</Label>
          <Input id="narrow-days" type="number" min={1} value={form.days}
                 onChange={(e) => set({ days: e.target.value })} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("sharing.narrowDialog.daysHint", { date: share.expires_at.slice(0, 10) })}
          </p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="narrow-people">{t("sharing.narrowDialog.people")}</Label>
          <Input id="narrow-people" type="number" min={Math.max(1, share.holders)}
                 max={share.max_people - 1} value={form.people}
                 onChange={(e) => set({ people: e.target.value })} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("sharing.narrowDialog.peopleHint", { max: share.max_people, holders: share.holders })}
          </p>
        </div>
      </div>

      {share.allow_ask && (
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox checked={form.askOff} onChange={(e) => set({ askOff: e.target.checked })} />
          {t("sharing.narrowDialog.askOff")}
        </label>
      )}

      {problem && (
        <p className="text-xs text-[var(--color-destructive)]">
          {t(problem, { max: MAX_DENY_GLOBS, holders: share.holders })}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onCancel}>{t("common.cancel")}</Button>
        <Button disabled={!!problem || !body || narrow.isPending} onClick={() => narrow.mutate()}>
          {t("sharing.narrow")}
        </Button>
      </DialogFooter>
    </div>
  );
}
