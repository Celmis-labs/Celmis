"use client";

/**
 * "Model prices" under a connected workspace LiteLLM proxy.
 *
 * A proxy alias names whatever the proxy maps it to — a fine-tune, a
 * self-hosted model, a vendor model LiteLLM's price table has never heard of
 * — so Celmis cannot always know what a call to it costs. Each alias is
 * priced, in order: a price a workspace admin sets here → the price the proxy
 * declares → LiteLLM's table price of the model behind the alias → unknown
 * (src/llm/proxy_pricing.py). This table shows which of those applies and
 * lets an admin fill in the gaps. Prices are USD per 1M tokens and apply to
 * calls made after they are saved.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { llmApi, type ModelPriceRow, type ModelPriceSource, type ModelPricesUpdate } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const MAX_PER_MTOK = 1000;

type Draft = { input: string; output: string };

const SOURCE_VARIANT: Record<ModelPriceSource, "brand" | "outline" | "default" | "warning"> = {
  manual: "brand",
  proxy: "outline",
  litellm: "default",
  unknown: "warning",
};

function fmt(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return `$${Number(v.toFixed(6))}`;
}

/** A typed value as a price, or null when it is not one. */
function parsePrice(raw: string): number | null {
  const s = raw.trim().replace(",", ".");
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 && n <= MAX_PER_MTOK ? n : null;
}

export function LiteLLMModelPrices({ fingerprint }: { fingerprint: string }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  const queryKey = ["litellm-prices", fingerprint];
  const prices = useQuery({
    queryKey,
    queryFn: () => llmApi.litellmPrices(token!),
    enabled: !!token,
    staleTime: 60_000,
  });

  const save = useMutation({
    mutationFn: (body: ModelPricesUpdate) => llmApi.saveLitellmPrices(token!, body),
    onSuccess: (data, body) => {
      qc.setQueryData(queryKey, data);
      setDrafts((d) => {
        const next = { ...d };
        for (const alias of Object.keys(body.prices)) delete next[alias];
        return next;
      });
      toast.success(t("llm.litellm.prices.saved"));
    },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });

  const refresh = useMutation({
    mutationFn: () => llmApi.litellmPrices(token!, true),
    onSuccess: (data) => qc.setQueryData(queryKey, data),
    onError: (e) => toast.error(t("llm.litellm.prices.loadError", { message: (e as Error).message })),
  });

  const data = prices.data;
  // The parent mounts this only for a connected proxy, so "no data yet" is
  // loading or a failed request — never "nothing to show".
  if (!data) {
    return (
      <div className="space-y-1 pt-2">
        <div className="text-sm font-medium">{t("llm.litellm.prices.title")}</div>
        {prices.isError ? (
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-[var(--color-destructive)]">
            <span>
              {t("llm.litellm.prices.loadError", { message: (prices.error as Error)?.message ?? "" })}
            </span>
            <Button size="sm" variant="outline" onClick={() => prices.refetch()}>
              {t("llm.litellm.prices.retry")}
            </Button>
          </div>
        ) : (
          <p className="text-[11px] text-[var(--color-muted-foreground)]">
            {t("llm.litellm.prices.loading")}
          </p>
        )}
      </div>
    );
  }
  if (!data.connected) return null;
  const canEdit = data.can_edit;

  const draftFor = (r: ModelPriceRow): Draft => drafts[r.alias] ?? {
    input: r.manual ? String(r.manual.input_per_mtok) : "",
    output: r.manual ? String(r.manual.output_per_mtok) : "",
  };
  const setDraft = (alias: string, patch: Partial<Draft>, current: Draft) =>
    setDrafts((d) => ({ ...d, [alias]: { ...current, ...patch } }));

  const sourceLabel = (s: ModelPriceSource) => t(`llm.litellm.prices.source.${s}`);

  return (
    <div className="space-y-2 pt-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-medium">{t("llm.litellm.prices.title")}</div>
        <div className="flex items-center gap-2">
          <div className="text-[11px] text-[var(--color-muted-foreground)]">
            {t("llm.litellm.prices.unit")}
          </div>
          <Button
            size="sm" variant="ghost"
            disabled={refresh.isPending || prices.isFetching}
            title={t("llm.litellm.prices.refreshHint")}
            onClick={() => refresh.mutate()}
          >
            {t("llm.litellm.prices.refresh")}
          </Button>
        </div>
      </div>
      <p className="text-[11px] text-[var(--color-muted-foreground)]">
        {t("llm.litellm.prices.intro")}
      </p>
      {data.detail && <p className="text-[11px] text-amber-600">{data.detail}</p>}
      {data.prices.length === 0 ? (
        <p className="text-[11px] text-[var(--color-muted-foreground)]">
          {t("llm.litellm.prices.empty")}
        </p>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>{t("llm.litellm.prices.col.alias")}</TH>
              <TH>{t("llm.litellm.prices.col.mode")}</TH>
              <TH>{t("llm.litellm.prices.col.underlying")}</TH>
              <TH>{t("llm.litellm.prices.col.input")}</TH>
              <TH>{t("llm.litellm.prices.col.output")}</TH>
              <TH>{t("llm.litellm.prices.col.source")}</TH>
              {canEdit && <TH>{t("llm.litellm.prices.col.manual")}</TH>}
            </TR>
          </THead>
          <TBody>
            {data.prices.map((r) => {
              const d = draftFor(r);
              const inp = parsePrice(d.input);
              const out = parsePrice(d.output);
              const dirty = drafts[r.alias] !== undefined;
              const valid = inp !== null && out !== null;
              const unknown = r.price_source === "unknown";
              return (
                <TR key={r.alias} className={unknown ? "bg-[var(--color-warning)]/10" : undefined}>
                  <TD className="font-mono break-all">
                    {r.alias}
                    {r.stale && (
                      <div className="text-[10px] text-[var(--color-muted-foreground)]">
                        {t("llm.litellm.prices.stale")}
                      </div>
                    )}
                    {unknown && !r.stale && (
                      <div className="text-[10px] text-[var(--color-warning)]">
                        {t("llm.litellm.prices.unknownHint")}
                      </div>
                    )}
                  </TD>
                  <TD>{r.mode ?? "—"}</TD>
                  <TD className="font-mono break-all">{r.underlying ?? "—"}</TD>
                  <TD>{fmt(r.input_per_mtok)}</TD>
                  <TD>{fmt(r.output_per_mtok)}</TD>
                  <TD>
                    <Badge variant={SOURCE_VARIANT[r.price_source]} className="text-[10px]">
                      {sourceLabel(r.price_source)}
                    </Badge>
                  </TD>
                  {canEdit && (
                    <TD>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Input
                          className="h-7 w-20 text-xs" inputMode="decimal"
                          aria-label={t("llm.litellm.prices.inputLabel", { alias: r.alias })}
                          placeholder={t("llm.litellm.prices.col.input")}
                          value={d.input} disabled={r.stale}
                          onChange={(e) => setDraft(r.alias, { input: e.target.value }, d)}
                        />
                        <Input
                          className="h-7 w-20 text-xs" inputMode="decimal"
                          aria-label={t("llm.litellm.prices.outputLabel", { alias: r.alias })}
                          placeholder={t("llm.litellm.prices.col.output")}
                          value={d.output} disabled={r.stale}
                          onChange={(e) => setDraft(r.alias, { output: e.target.value }, d)}
                        />
                        <Button
                          size="sm" variant="outline"
                          disabled={!dirty || !valid || save.isPending || r.stale}
                          onClick={() => save.mutate({ prices: {
                            [r.alias]: { input_per_mtok: inp!, output_per_mtok: out! },
                          } })}
                        >
                          {t("llm.litellm.prices.save")}
                        </Button>
                        <Button
                          size="sm" variant="ghost"
                          disabled={!r.manual || save.isPending}
                          title={t("llm.litellm.prices.resetHint")}
                          onClick={() => save.mutate({ prices: { [r.alias]: null } })}
                        >
                          {t("llm.litellm.prices.reset")}
                        </Button>
                      </div>
                      {dirty && !valid && (
                        <div className="text-[10px] text-[var(--color-destructive)] pt-1">
                          {t("llm.litellm.prices.invalid", { max: MAX_PER_MTOK })}
                        </div>
                      )}
                    </TD>
                  )}
                </TR>
              );
            })}
          </TBody>
        </Table>
      )}
      <p className="text-[11px] text-[var(--color-muted-foreground)]">
        {canEdit ? t("llm.litellm.prices.appliesForward") : t("llm.litellm.prices.readOnly")}
      </p>
    </div>
  );
}
