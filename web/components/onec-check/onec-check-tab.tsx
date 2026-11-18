"use client";

/**
 * "Check a file": upload a 1C external file, pick a configuration (and
 * optionally a model for the wording), get the report, download it.
 * One component, used as a tab on the Q&A page and on the agent page.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { DownloadIcon, FileUpIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { ConfigPicker } from "@/components/onec-check/config-picker";
import { ConnectMcpDialog } from "@/components/onec-check/connect-dialog";
import { ReportView } from "@/components/onec-check/report-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, type SelectOption } from "@/components/ui/select";
import { downloadWithAuth, onecReviewApi, type OnecConfig, type OnecFile, type OnecReport } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";

const ACCEPT = ".erf,.epf,.bin";
const LEVEL_VARIANT = { bad: "destructive", warn: "warning", good: "success", unknown: "outline" } as const;

export function OnecCheckTab() {
  const { t, locale } = useI18n();
  const token = useToken();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<OnecFile | null>(null);
  const [dragging, setDragging] = useState(false);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<OnecConfig | null>(null);
  const config = picked?.slug ?? "";
  const [model, setModel] = useState("none");
  const [lang, setLang] = useState(locale === "uk" ? "uk" : "en");
  const [report, setReport] = useState<OnecReport | null>(null);

  // One request per pause in typing, not per key.
  useEffect(() => {
    const id = setTimeout(() => setQuery(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);
  const configs = useQuery({
    queryKey: ["onec-configs", query],
    queryFn: () => onecReviewApi.configs(token!, query),
    enabled: !!token,
    placeholderData: (prev) => prev,
  });
  const models = useQuery({
    queryKey: ["onec-models"],
    queryFn: () => onecReviewApi.models(token!),
    enabled: !!token,
    staleTime: 5 * 60_000,
  });

  const upload = useMutation({
    mutationFn: (f: File) => onecReviewApi.upload(token!, f),
    onSuccess: (r) => { setFile(r); setReport(null); },
    onError: (e) => toast.error((e as Error).message),
  });
  const run = useMutation({
    mutationFn: () => onecReviewApi.run(token!, {
      handle: file!.handle, config, lang,
      model: model === "none" ? "" : model,
    }),
    onSuccess: (r) => setReport(r),
    onError: (e) => toast.error(`${t("onecCheck.failed")}: ${(e as Error).message}`),
  });

  const pick = (f: File | undefined) => { if (f) upload.mutate(f); };

  const d = models.data;
  const modelOptions: SelectOption[] = [{ value: "none", label: t("onecCheck.modelNone") }];
  if (d?.default_available) {
    modelOptions.push({
      value: "default",
      label: t("onecCheck.modelDefault", { model: `${d.default_provider}:${d.default_model}` }),
    });
  }
  for (const m of d?.models ?? []) {
    modelOptions.push({ value: `${m.provider}:${m.model}`, label: m.model, group: m.provider });
  }
  // Nothing to pick: say so and where to fix it, instead of a select with one option.
  const noModel = models.isSuccess && !d?.default_available && (d?.models.length ?? 0) === 0;

  const download = async (format: "pdf" | "docx" | "md") => {
    try {
      await downloadWithAuth(onecReviewApi.exportUrl(report!.id, format), `report.${format}`, token);
    } catch (e) {
      toast.error(`${t("onecCheck.exportFailed")}: ${(e as Error).message}`);
    }
  };

  return (
    <div className="space-y-4" data-testid="onec-check-tab">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="text-base">{t("onecCheck.title")}</CardTitle>
            <CardDescription>{t("onecCheck.description")}</CardDescription>
          </div>
          <ConnectMcpDialog />
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>{t("onecCheck.fileLabel")}</Label>
            <div
              role="button"
              tabIndex={0}
              data-testid="onec-drop"
              onClick={() => input.current?.click()}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files[0]); }}
              className={cn(
                "mt-1.5 flex cursor-pointer flex-col items-center gap-1 rounded-lg border border-dashed px-4 py-6 text-center text-sm",
                "border-[var(--color-border-strong)] hover:bg-[var(--color-muted)]/40",
                dragging && "bg-[var(--color-muted)]/60",
              )}
            >
              {upload.isPending
                ? <Loader2Icon className="h-5 w-5 animate-spin" />
                : <FileUpIcon className="h-5 w-5" />}
              <span>
                {upload.isPending ? t("onecCheck.uploading")
                  : file ? file.file_name : t("onecCheck.drop")}
              </span>
              {file && !upload.isPending && (
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {t("onecCheck.uploaded", {
                    kind: file.kind, name: file.object_name, forms: file.forms, templates: file.templates,
                  })}
                </span>
              )}
              <input
                ref={input} type="file" accept={ACCEPT} className="hidden" data-testid="onec-input"
                onChange={(e) => { pick(e.target.files?.[0]); e.target.value = ""; }}
              />
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="onec-config">{t("onecCheck.configLabel")}</Label>
              <ConfigPicker
                id="onec-config" value={config} selected={picked} onPick={setPicked}
                query={search} onQuery={setSearch} options={configs.data ?? []}
                loading={configs.isFetching}
              />
              {configs.isSuccess && configs.data.length === 0 && !query && (
                <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.configEmpty")}</p>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="onec-model">{t("onecCheck.modelLabel")}</Label>
              <Select id="onec-model" value={model} onChange={setModel} options={modelOptions} />
              {noModel ? (
                <p className="text-xs text-[var(--color-muted-foreground)]" data-testid="onec-no-model">
                  {t("onecCheck.modelNoneConnected")}{" "}
                  <Link href="/settings/llm" className="underline underline-offset-2">
                    {t("onecCheck.modelConnectLink")}
                  </Link>
                  .
                </p>
              ) : (
                <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.modelHint")}</p>
              )}
              <Label htmlFor="onec-lang" className="pt-1">{t("onecCheck.langLabel")}</Label>
              <Select
                id="onec-lang" value={lang} onChange={setLang}
                options={[{ value: "uk", label: "Українська" }, { value: "en", label: "English" }]}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" onClick={() => run.mutate()} disabled={!file || run.isPending || upload.isPending}>
              {run.isPending && <Loader2Icon className="mr-1.5 h-4 w-4 animate-spin" />}
              {run.isPending ? t("onecCheck.running") : t("onecCheck.run")}
            </Button>
            {run.isPending && (
              <span className="text-xs text-[var(--color-muted-foreground)]" role="status">
                {t("onecCheck.progress")}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {report ? (
        <Card data-testid="onec-result">
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={LEVEL_VARIANT[report.level]}>{t(`onecCheck.level.${report.level}`)}</Badge>
              {report.model.used && (
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {t("onecCheck.modelUsed", { label: report.model.label })}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.export")}</span>
              {(["pdf", "docx", "md"] as const).map((f) => (
                <Button key={f} type="button" variant="outline" size="sm" onClick={() => download(f)}>
                  <DownloadIcon className="mr-1 h-3.5 w-3.5" /> {f.toUpperCase()}
                </Button>
              ))}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {report.model.mode !== "none" && !report.model.used && (
              <Callout tone="warning">{t("onecCheck.modelFailed", { failure: report.model.failure })}</Callout>
            )}
            <ReportView blocks={report.blocks} />
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.kept")}</p>
          </CardContent>
        </Card>
      ) : (
        !run.isPending && (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("onecCheck.empty")}</p>
        )
      )}
    </div>
  );
}
