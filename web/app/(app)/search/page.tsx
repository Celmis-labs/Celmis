"use client";

/**
 * /search — cross-repo symbol + semantic docs search (Stage 21).
 * Two sections: exact code symbols from the tree-sitter graphs, and
 * semantic vault-note matches from Qdrant.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { SearchIcon } from "lucide-react";

import { searchApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { WorkspaceBadge } from "@/components/workspace-badge";

export default function SearchPage() {
  const t = useT();
  const token = useToken();
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");

  // Deep links (?q=…) from chat citations pre-fill and run the search.
  // Read via window.location to avoid a useSearchParams Suspense boundary;
  // deferred a tick so the state update happens outside the effect body.
  useEffect(() => {
    const q0 = new URLSearchParams(window.location.search).get("q");
    if (!q0?.trim()) return;
    const id = setTimeout(() => {
      setInput(q0);
      setQuery(q0.trim());
    }, 0);
    return () => clearTimeout(id);
  }, []);

  const q = useQuery({
    queryKey: ["search", query],
    queryFn: () => searchApi.search(token!, query),
    enabled: !!token && query.length >= 2,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<SearchIcon className="h-6 w-6" />}
        title={t("search.title")}
        badge={<WorkspaceBadge />}
        description={t("search.description")}
        tabs={<SectionTabs set="qa" />}
      />

      <form
        onSubmit={(e) => { e.preventDefault(); setQuery(input.trim()); }}
        className="flex gap-2"
      >
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t("search.placeholder")}
          autoFocus
        />
        <Button type="submit" disabled={input.trim().length < 2}>
          <SearchIcon className="h-4 w-4 mr-1" /> {t("search.submit")}
        </Button>
      </form>

      {q.isLoading && <div className="text-sm">{t("search.searching")}</div>}
      {q.error && (
        <div className="text-sm text-red-600">{(q.error as Error).message}</div>
      )}

      {q.data && (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                {t("search.codeSymbols", { count: q.data.symbol_count })}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-1">
              {q.data.symbols_error && (
                <p className="text-xs text-red-600">
                  {t("search.symbolsError", { error: q.data.symbols_error })}
                </p>
              )}
              {q.data.symbols.length === 0 && (
                <p className="text-sm text-[var(--color-muted-foreground)]">{t("search.noSymbols")}</p>
              )}
              {q.data.symbols.map((s, i) => (
                <div key={i} className="border border-[var(--color-border)] rounded p-2 text-sm">
                  <div className="flex items-center gap-2 flex-wrap">
                    <code className="font-semibold">{s.name}</code>
                    <Badge variant="outline">{s.kind}</Badge>
                    {s.language && <Badge variant="outline">{s.language}</Badge>}
                    <Badge variant="outline">{s.repo_slug}</Badge>
                  </div>
                  <div className="text-xs text-[var(--color-muted-foreground)] font-mono mt-1">
                    {s.web_url ? (
                      <a href={s.web_url} target="_blank" rel="noreferrer"
                         title={t("search.openFile")} className="hover:underline">
                        {s.file}:{s.line}
                      </a>
                    ) : (
                      <>{s.file}:{s.line}</>
                    )}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                {t("search.docsSemantic", { count: q.data.note_count })}
              </CardTitle>
              <CardDescription>{t("search.docsDescription")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-1">
              {q.data.notes_error === "vault-not-generated" ? (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("search.docsNotGenerated")}
                </p>
              ) : q.data.notes_error ? (
                <p className="text-xs text-red-600">
                  {t("search.docsError", { error: q.data.notes_error })}
                </p>
              ) : null}
              {q.data.notes.length === 0 && (
                <p className="text-sm text-[var(--color-muted-foreground)]">{t("search.noDocs")}</p>
              )}
              {q.data.notes.map((n, i) => (
                <div key={i} className="border border-[var(--color-border)] rounded p-2 text-sm">
                  <div className="flex items-center gap-2 flex-wrap">
                    {n.web_url ? (
                      <a href={n.web_url} target="_blank" rel="noreferrer"
                         title={t("search.openFile")} className="hover:underline">
                        <code>{n.note_path}</code>
                      </a>
                    ) : (
                      <code>{n.note_path}</code>
                    )}
                    <Badge variant="outline">{n.type}</Badge>
                    <Badge variant="outline">{n.repo}</Badge>
                    <span className="ml-auto text-xs text-[var(--color-muted-foreground)]">
                      {t("search.score", { score: n.score })}
                    </span>
                  </div>
                  {n.keywords?.length > 0 && (
                    <div className="text-xs text-[var(--color-muted-foreground)] mt-1">
                      {n.keywords.join(" · ")}
                    </div>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </PageShell>
  );
}
