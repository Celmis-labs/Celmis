"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * The two licensed acts of repository sharing: making a share and giving more of a
 * running one (`POST /api/sharing-grants` and `.../{id}/extend`, the licence feature
 * `sharing`). Seeing a share, narrowing it, blocking a person and ending it are free
 * and live on the page itself (`app/(app)/admin/sharing`); this file is only what
 * widens what another workspace may read.
 *
 * The page draws these dialogs only when `/api/capabilities` says
 * `repo_sharing_grant` is available. A licence that lapses while the page is open is
 * a 403 from the API, and the dialog says so in the words of the licence note rather
 * than as a bare refusal.
 *
 * The dialog owns nothing the server does not decide again: the limits it checks
 * (days, people, number of paths) are the server's, kept in `lib/sharing.ts`.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  api, ApiError, mcpTokensApi, sharingApi, workspacesApi,
  type RepoOut, type ShareDetail,
} from "@/lib/api";
import {
  MAX_DENY_GLOBS, MAX_PEOPLE, createBody, createProblem, emptyCreate, extendBody, extendFormOf,
  extendProblem, refusalKey, workspaceOptions, type CreateForm, type ExtendForm,
} from "@/lib/sharing";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

/** Two number fields side by side. On a wide screen each field spans the same three
 *  rows (label, box, hint) of the shared grid, so the boxes line up even when one
 *  label wraps onto a second line in a longer language; on a phone they simply stack. */
const NUMBER_ROW = "grid gap-x-3 gap-y-3 sm:grid-cols-2 sm:gap-y-0";
const NUMBER_FIELD = "grid content-start gap-y-1 sm:row-span-3 sm:grid-rows-subgrid";

/** What the server allows a share to last: the ceiling a token has. The server
 *  checks it again, so a failed read of it only costs the hint. */
const FALLBACK_MAX_DAYS = 90;

function useMaxDays(): number {
  const token = useToken();
  const mine = useQuery({
    queryKey: ["mcp-tokens-me"],
    queryFn: () => mcpTokensApi.mine(token!),
    enabled: !!token,
    staleTime: 5 * 60 * 1000,
  });
  return mine.data?.max_days ?? FALLBACK_MAX_DAYS;
}

/** The sentence for a refused call: the reader's language when the refusal is one
 *  the page knows, the server's own words otherwise. */
function useRefusal() {
  const t = useT();
  return (e: unknown): string => {
    if (e instanceof ApiError) {
      const key = refusalKey(e.status, e.message, true);
      if (key) return t(key);
    }
    return e instanceof Error ? e.message : String(e);
  };
}

// ─── make a share ────────────────────────────────────────────────────

export function CreateShareDialog({
  open, onOpenChange, onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the new share once it exists. */
  onDone: (share: ShareDetail) => void;
}) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("sharing.dialog.title")}</DialogTitle>
          <DialogDescription>{t("sharing.dialog.intro")}</DialogDescription>
        </DialogHeader>
        {/* Inside the content so that it is built afresh each time the dialog opens. */}
        <CreateShareForm onCancel={() => onOpenChange(false)}
                         onDone={(s) => { onOpenChange(false); onDone(s); }} />
      </DialogContent>
    </Dialog>
  );
}

function CreateShareForm({
  onCancel, onDone,
}: { onCancel: () => void; onDone: (share: ShareDetail) => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const refusal = useRefusal();
  const maxDays = useMaxDays();
  const [form, setForm] = useState<CreateForm>(emptyCreate);
  // A form that has just opened is not an error: the sentence about a missing choice
  // is shown only once the person has changed something. The button stays off until
  // there is nothing to say.
  const [touched, setTouched] = useState(false);
  const set = (patch: Partial<CreateForm>) => {
    setTouched(true);
    setForm((cur) => ({ ...cur, ...patch }));
  };

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  // The receiving workspace is picked from a list when the server offers one: the
  // owner's administrator is usually not a member of it, so nothing they could already
  // read names it. The server offers it only where an installation's workspaces are one
  // organisation (a 404 otherwise, or a 403 once the licence has lapsed), and then the
  // workspace is typed.
  const targets = useQuery({
    queryKey: ["sharing-targets"],
    queryFn: () => sharingApi.workspaces(token!),
    enabled: !!token,
    retry: false,
    staleTime: 60 * 1000,
  });
  const typed = targets.isError;
  // Typed, the workspaces the person belongs to are suggestions for the slug.
  const mine = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token && typed,
  });
  const others = (mine.data?.workspaces ?? []).filter((w) => w.id !== mine.data?.active_id);
  const choices = workspaceOptions(targets.data ?? []);

  const problem = createProblem(form, maxDays);
  const create = useMutation({
    mutationFn: () => sharingApi.create(token!, createBody(form)),
    onSuccess: (share) => {
      toast.success(t("sharing.created"));
      qc.invalidateQueries({ queryKey: ["sharing-shares"] });
      onDone(share);
    },
    onError: (e: Error) => toast.error(refusal(e)),
  });

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <Label htmlFor="share-repo">{t("sharing.dialog.repo")}</Label>
        <Select id="share-repo" className="w-full" value={form.repo} onChange={(v) => set({ repo: v })}
                placeholder={t("common.select")}
                options={(repos.data ?? []).map((r) => ({
                  value: r.slug, label: r.full_name || r.slug,
                }))} />
      </div>

      {typed ? (
        <div className="space-y-1">
          <Label htmlFor="share-workspace">{t("sharing.dialog.workspace")}</Label>
          <Input id="share-workspace" list="share-workspace-options" value={form.workspace}
                 onChange={(e) => set({ workspace: e.target.value })}
                 autoComplete="off" spellCheck={false} />
          <datalist id="share-workspace-options">
            {others.map((w) => <option key={w.id} value={w.slug}>{w.name}</option>)}
          </datalist>
        </div>
      ) : (
        <div className="space-y-1">
          <Label htmlFor="share-workspace">{t("sharing.dialog.workspacePick")}</Label>
          <Select id="share-workspace" className="w-full" value={form.workspace}
                  onChange={(v) => set({ workspace: v })} placeholder={t("common.select")}
                  disabled={targets.isPending || choices.length === 0} options={choices} />
          {targets.isSuccess && choices.length === 0 && (
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("sharing.dialog.workspaceNone")}
            </p>
          )}
        </div>
      )}

      <div className={NUMBER_ROW}>
        <div className={NUMBER_FIELD}>
          <Label htmlFor="share-days">{t("sharing.dialog.days")}</Label>
          <Input id="share-days" type="number" min={1} max={maxDays} value={form.days}
                 onChange={(e) => set({ days: e.target.value })} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("sharing.dialog.daysHint", { max: maxDays })}
          </p>
        </div>
        <div className={NUMBER_FIELD}>
          <Label htmlFor="share-people">{t("sharing.dialog.people")}</Label>
          <Input id="share-people" type="number" min={1} max={MAX_PEOPLE} value={form.people}
                 onChange={(e) => set({ people: e.target.value })} />
          <span aria-hidden="true" />
        </div>
      </div>

      <div className="space-y-1">
        <Label htmlFor="share-deny">{t("sharing.dialog.deny")}</Label>
        <Textarea id="share-deny" rows={3} className="font-mono text-xs" value={form.deny}
                  onChange={(e) => set({ deny: e.target.value })} spellCheck={false} />
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("sharing.dialog.denyHint")}</p>
      </div>

      <div className="space-y-2">
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox checked={form.ask} onChange={(e) => set({ ask: e.target.checked })} />
          {t("sharing.dialog.ask")}
        </label>
        {form.ask && <Callout tone="warning">{t("sharing.dialog.askWarn")}</Callout>}
      </div>

      <Callout tone="info">{t("sharing.dialog.privacy")}</Callout>
      {touched && problem && (
        <p className="text-xs text-[var(--color-destructive)]">
          {t(problem, { max: problem === "sharing.dialog.badPeople" ? MAX_PEOPLE
            : problem === "sharing.dialog.tooManyPaths" ? MAX_DENY_GLOBS : maxDays })}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onCancel}>{t("common.cancel")}</Button>
        <Button disabled={!!problem || create.isPending} onClick={() => create.mutate()}>
          {t("sharing.dialog.submit")}
        </Button>
      </DialogFooter>
    </div>
  );
}

// ─── give more of a share ────────────────────────────────────────────

export function ExtendShareDialog({
  share, open, onOpenChange, onDone,
}: {
  /** The share with its hidden paths: the form starts from what it is now. */
  share: ShareDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (share: ShareDetail) => void;
}) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("sharing.extendDialog.title")}</DialogTitle>
          <DialogDescription>{t("sharing.extendDialog.intro")}</DialogDescription>
        </DialogHeader>
        <ExtendShareForm share={share} onCancel={() => onOpenChange(false)}
                         onDone={(s) => { onOpenChange(false); onDone(s); }} />
      </DialogContent>
    </Dialog>
  );
}

function ExtendShareForm({
  share, onCancel, onDone,
}: { share: ShareDetail; onCancel: () => void; onDone: (share: ShareDetail) => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const refusal = useRefusal();
  const maxDays = useMaxDays();
  const [form, setForm] = useState<ExtendForm>(() => extendFormOf(share));
  const set = (patch: Partial<ExtendForm>) => setForm((cur) => ({ ...cur, ...patch }));

  const problem = extendProblem(form, share, maxDays);
  const body = extendBody(form, share);
  const extend = useMutation({
    mutationFn: () => sharingApi.extend(token!, share.share_id, body!),
    onSuccess: (updated) => {
      toast.success(t("sharing.extended"));
      qc.invalidateQueries({ queryKey: ["sharing-shares"] });
      onDone(updated);
    },
    onError: (e: Error) => toast.error(refusal(e)),
  });

  return (
    <div className="space-y-4">
      <div className={NUMBER_ROW}>
        <div className={NUMBER_FIELD}>
          <Label htmlFor="extend-days">{t("sharing.extendDialog.days")}</Label>
          <Input id="extend-days" type="number" min={1} max={maxDays} value={form.days}
                 onChange={(e) => set({ days: e.target.value })} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("sharing.extendDialog.daysHint", {
              date: share.expires_at.slice(0, 10), max: maxDays,
            })}
          </p>
        </div>
        <div className={NUMBER_FIELD}>
          <Label htmlFor="extend-people">{t("sharing.dialog.people")}</Label>
          <Input id="extend-people" type="number" min={1} max={MAX_PEOPLE} value={form.people}
                 onChange={(e) => set({ people: e.target.value })} />
          <span aria-hidden="true" />
        </div>
      </div>

      <div className="space-y-1">
        <Label htmlFor="extend-deny">{t("sharing.extendDialog.deny")}</Label>
        <Textarea id="extend-deny" rows={4} className="font-mono text-xs" value={form.deny}
                  onChange={(e) => set({ deny: e.target.value })} spellCheck={false} />
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("sharing.dialog.denyHint")}</p>
      </div>

      <div className="space-y-2">
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox checked={form.ask} onChange={(e) => set({ ask: e.target.checked })} />
          {t("sharing.dialog.ask")}
        </label>
        {form.ask && !share.allow_ask && (
          <Callout tone="warning">{t("sharing.dialog.askWarn")}</Callout>
        )}
      </div>

      {problem && (
        <p className="text-xs text-[var(--color-destructive)]">
          {t(problem, { max: problem === "sharing.dialog.badPeople" ? MAX_PEOPLE
            : problem === "sharing.dialog.tooManyPaths" ? MAX_DENY_GLOBS : maxDays })}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onCancel}>{t("common.cancel")}</Button>
        <Button disabled={!!problem || !body || extend.isPending} onClick={() => extend.mutate()}>
          {t("sharing.extend")}
        </Button>
      </DialogFooter>
    </div>
  );
}
