"use client";

/**
 * New pack: a name, who sees it, and optionally the teams it starts with.
 * The pack is created empty and opens straight on its page, where the first
 * upload is the next step; the person who creates it becomes its maintainer.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { codebaseApi, teamsApi, type PackAudience } from "@/lib/api";
import { slugify } from "@/lib/codebase";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { AudienceGuide, AudiencePicker } from "@/components/codebase/audience";
import { TeamGrantsEditor, type TeamGrant } from "@/components/codebase/team-grants";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { InlineHelp } from "@/components/ui/inline-help";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export function CreatePackDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("codebase.create.title")}</DialogTitle>
          <DialogDescription>{t("codebase.create.desc")}</DialogDescription>
        </DialogHeader>
        <CreateForm onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

function CreateForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  const token = useToken();
  const router = useRouter();
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  // The slug follows the title until the person types their own.
  const [slugDraft, setSlugDraft] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [audience, setAudience] = useState<PackAudience>("teams");
  const [grants, setGrants] = useState<TeamGrant[]>([]);
  // What is sent: the typed text cleaned the same way a suggestion is, so
  // "My Pack!" and "my--pack" both arrive as "my-pack" and the hint below the
  // field shows exactly that.
  const slug = slugify(slugDraft ?? title);

  const teams = useQuery({
    queryKey: ["codebase", "teams"],
    queryFn: () => teamsApi.list(token!),
    enabled: !!token,
  });

  const create = useMutation({
    mutationFn: () => codebaseApi.create(token!, {
      slug, title: title.trim(), description: description.trim(), audience, teams: grants,
    }),
    onSuccess: (pack) => {
      qc.invalidateQueries({ queryKey: ["codebase"] });
      toast.success(t("codebase.create.created", { title: pack.title }));
      onClose();
      router.push(`/codebase/packs/${pack.id}`);
    },
  });

  return (
    <form
      className="min-w-0 space-y-4"
      onSubmit={(e) => { e.preventDefault(); if (title.trim() && slug && token) create.mutate(); }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="pack-title">{t("codebase.create.name")}</Label>
        <Input
          id="pack-title" value={title} maxLength={200} autoFocus
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("codebase.create.namePlaceholder")}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="pack-slug">{t("codebase.create.slug")}</Label>
        <Input
          id="pack-slug" value={slugDraft ?? slug} maxLength={64} className="font-mono"
          onChange={(e) => setSlugDraft(e.target.value.toLowerCase())}
          placeholder={t("codebase.create.slugPlaceholder")}
        />
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("codebase.create.slugHint", { slug: slug || "…" })}
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="pack-description">{t("codebase.create.description")}</Label>
        <Textarea
          id="pack-description" rows={2} maxLength={2000} value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t("codebase.create.descriptionPlaceholder")}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="pack-audience">{t("codebase.create.audience")}</Label>
        <AudiencePicker id="pack-audience" value={audience} onChange={setAudience} teamCount={grants.length} />
        <InlineHelp question={t("codebase.guide.title")} className="pt-1">
          <AudienceGuide current={audience} />
        </InlineHelp>
      </div>
      <div className="space-y-1.5">
        <Label>{t("codebase.create.teams")}</Label>
        <TeamGrantsEditor value={grants} onChange={setGrants} teams={teams.data ?? []} />
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.create.maintainerNote")}</p>
      </div>

      {create.isError && <Callout tone="danger">{(create.error as Error).message}</Callout>}

      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
        <Button type="submit" loading={create.isPending} disabled={!title.trim() || !slug || !token}>
          {t("codebase.create.submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}
