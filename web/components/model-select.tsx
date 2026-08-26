"use client";

/**
 * ModelSelect — autocomplete combobox for LLM model selection.
 *
 * Behaviour:
 *   - Input is free-typeable so users can enter any LiteLLM-format model
 *     string. Autocomplete narrows to matching entries as they type.
 *   - Models from providers the user has NOT connected are visually dimmed
 *     and shown with a "no key" tag — clicking still allows selection, but
 *     the runtime resolver will fail unless the user adds the key.
 *   - Live cost preview shown as "$3.00 in / $15.00 out per M".
 *   - The empty string represents "inherit workspace default" — that's the
 *     first item in the list and the default when the field is cleared.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckIcon, ChevronDownIcon, ChevronUpIcon, XCircleIcon } from "lucide-react";

import { modelsApi, type ModelInfo } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";

export function ModelSelect({
  value,
  onChange,
  placeholder = "inherit workspace default",
  onlyAvailable = false,
  disabled = false,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  /**
   * When true, hide models whose provider has no connected key. Use on
   * pages where a fallback to "raw string" would surprise the user (repo
   * policy — a wrong per-agent override silently degrades reviews).
   */
  onlyAvailable?: boolean;
  /**
   * Read-only. The workspace LLM page renders this for viewers who may not
   * edit, and for the Claude Code engine, where the choice exists but does
   * not apply — both want the current value legible, not gone.
   */
  disabled?: boolean;
}) {
  const token = useToken();
  const t = useT();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const catalog = useQuery({
    queryKey: ["models", "available", "all"],
    queryFn: () => modelsApi.available(token!, { allProviders: true }),
    enabled: !!token,
    staleTime: 5 * 60_000,
  });

  const filtered = useMemo(() => {
    const q = query.toLowerCase().trim();
    let all = catalog.data?.models ?? [];
    if (onlyAvailable) all = all.filter((m) => m.available);
    if (!q) return all;
    return all.filter((m) =>
      m.id.toLowerCase().includes(q) ||
      m.provider.toLowerCase().includes(q) ||
      (m.recommended_for ?? "").toLowerCase().includes(q),
    );
  }, [catalog.data, query, onlyAvailable]);

  const selected = catalog.data?.models.find((m) => m.id === value);

  return (
    <div className="relative">
      <div className="flex items-center gap-2">
        <Input
          value={open ? query : value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => {
            setQuery(e.target.value);
            onChange(e.target.value);
            if (!open) setOpen(true);
          }}
          onFocus={() => {
            if (disabled) return;
            setQuery(value);
            setOpen(true);
          }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          className={value === "" ? "italic" : ""}
        />
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
          className="rounded p-1 hover:bg-[var(--color-accent)] disabled:cursor-not-allowed disabled:opacity-50"
          aria-label={open ? "Close list" : "Open list"}
        >
          {open ? (
            <ChevronUpIcon className="h-4 w-4 opacity-60" />
          ) : (
            <ChevronDownIcon className="h-4 w-4 opacity-60" />
          )}
        </button>
        {value && !open && !disabled && (
          <button
            type="button"
            onClick={() => {
              onChange("");
              setQuery("");
            }}
            className="rounded p-1 hover:bg-[var(--color-accent)]"
            aria-label={t("settings.models.clearOverride")}
            title={t("settings.models.resetToDefault")}
          >
            <XCircleIcon className="h-4 w-4 opacity-60" />
          </button>
        )}
      </div>

      {selected && !open && (
        <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">
          ${selected.input_per_m.toFixed(2)}/M in · $
          {selected.output_per_m.toFixed(2)}/M out
          {selected.max_context && (
            <> · {(selected.max_context / 1000).toFixed(0)}K ctx</>
          )}
          {!selected.available && (
            <> · <span className="text-amber-600">no key — will fail at runtime</span></>
          )}
        </div>
      )}

      {open && (
        <div
          className="absolute z-20 mt-1 w-full max-h-72 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-card)] shadow-lg"
          onMouseDown={(e) => e.preventDefault()}
        >
          <Row
            active={value === ""}
            onClick={() => {
              onChange("");
              setQuery("");
              setOpen(false);
            }}
            label="(inherit workspace default)"
            meta={<span className="italic opacity-60">no override</span>}
          />
          {catalog.isLoading && <Row disabled label="Loading catalog…" />}
          {!catalog.isLoading && filtered.length === 0 && (
            <Row disabled label={
              query
                ? `No models matching "${query}" — pressing Enter will save the raw string`
                : "No models found"
            } />
          )}
          {filtered.slice(0, 200).map((m) => (
            <Row
              key={m.id}
              active={m.id === value}
              onClick={() => {
                onChange(m.id);
                setQuery("");
                setOpen(false);
              }}
              label={m.id}
              meta={
                <div className="flex items-center gap-1.5 text-xs">
                  <span>
                    ${m.input_per_m.toFixed(2)}/${m.output_per_m.toFixed(2)}
                  </span>
                  {!m.available && (
                    <Badge variant="outline" className="text-[9px] px-1 py-0">
                      no key
                    </Badge>
                  )}
                  {m.recommended_for && (
                    <Badge variant="outline" className="text-[9px] px-1 py-0">
                      {m.recommended_for}
                    </Badge>
                  )}
                </div>
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}


function Row({
  active,
  onClick,
  disabled,
  label,
  meta,
}: {
  active?: boolean;
  onClick?: () => void;
  disabled?: boolean;
  label: string;
  meta?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`w-full text-left flex items-center gap-2 px-3 py-2 text-sm hover:bg-[var(--color-accent)] disabled:opacity-50 disabled:hover:bg-transparent ${
        active ? "bg-[var(--color-accent)]" : ""
      }`}
    >
      <span className="flex-shrink-0 w-4">
        {active && <CheckIcon className="h-3.5 w-3.5" />}
      </span>
      <span className="flex-1 truncate">{label}</span>
      {meta && <div className="flex-shrink-0 text-[var(--color-muted-foreground)]">{meta}</div>}
    </button>
  );
}
