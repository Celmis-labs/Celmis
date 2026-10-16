"use client";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangleIcon } from "lucide-react";
import { api, type ScopeCatalogue, type ScopeNeed } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

export type MissingScopes = {
  /** Keys (or whole entries) of required scopes the token lacks. */
  required?: (string | { key: string })[];
  optional?: (string | { key: string })[];
};

const keyOf = (m: string | { key: string }) => (typeof m === "string" ? m : m.key);

/**
 * The permissions a git token needs, as the provider's token page names them,
 * with the ones a verify (or the saved connection) found missing highlighted.
 * `kind` is the saved/verified token kind; without it the provider's default
 * kind is listed.
 */
export function TokenScopes({
  provider,
  kind,
  missing,
  scopesChecked,
}: {
  provider: string;
  kind?: string | null;
  missing?: MissingScopes;
  /** false after a verify = the provider did not say what the token can do. */
  scopesChecked?: boolean;
}) {
  const token = useToken();
  const t = useT();
  const cat = useQuery({
    queryKey: ["connection-scopes"],
    queryFn: () => api<ScopeCatalogue>("/api/connections/scopes", { token }),
    enabled: !!token,
    staleTime: 10 * 60 * 1000,
  });
  if (!cat.data) return null;
  const effective =
    kind && cat.data.kinds[kind] ? kind : cat.data.default_kind[provider];
  const needs: ScopeNeed[] = (effective && cat.data.kinds[effective]) || [];
  if (needs.length === 0) return null;
  const req = new Set((missing?.required ?? []).map(keyOf));
  const opt = new Set((missing?.optional ?? []).map(keyOf));

  return (
    <div data-tour={`connection-scopes:${provider}`} className="text-sm">
      <div className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">
        {t("connections.scopesTitle")}
      </div>
      {scopesChecked === false && (
        <p className="mb-2 flex items-start gap-1.5 text-xs text-[var(--color-warning)]">
          <AlertTriangleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {t("connections.scopesUnreadable")}
        </p>
      )}
      <ul className="space-y-1">
        {needs.map((n) => {
          const lacksReq = n.required && req.has(n.key);
          const lacksOpt = !n.required && opt.has(n.key);
          const purpose = t(`connections.scope.${n.key}`);
          return (
            <li key={n.key} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <code
                className={`rounded bg-[var(--color-secondary)] px-1 py-0.5 text-xs ${
                  lacksReq ? "text-[var(--color-destructive)]" : ""
                }`}
              >
                {n.label}
              </code>
              <Badge variant={n.required ? "default" : "outline"}>
                {n.required ? t("connections.scopeRequired") : t("connections.scopeOptional")}
              </Badge>
              {lacksReq && <Badge variant="destructive">{t("connections.scopeMissing")}</Badge>}
              {lacksOpt && <Badge variant="warning">{t("connections.scopeMissing")}</Badge>}
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {purpose === `connections.scope.${n.key}` ? n.purpose : purpose}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Labels of the required scopes a verify result found missing, for a toast. */
export const missingLabels = (list: { label: string }[] | undefined) =>
  (list ?? []).map((m) => m.label).join(", ");
