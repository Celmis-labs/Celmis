"use client";

/**
 * Renders a 1C file report: the list of blocks the server built
 * (`src/docs/blocks.py`). Inline markup is the small set the blocks use:
 * **bold**, `code`, *italic*. Nothing is injected as HTML.
 */

import { Fragment } from "react";

import type { ReportBlock } from "@/lib/api";
import { cn } from "@/lib/utils";

const INLINE = /(\*\*[^*\n]+?\*\*|`[^`\n]+`|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*]))/g;

function Inline({ text }: { text: string }) {
  const parts = text.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").split(INLINE);
  return (
    <>
      {parts.map((p, i) => {
        if (p.startsWith("**") && p.endsWith("**") && p.length > 4) return <strong key={i}>{p.slice(2, -2)}</strong>;
        if (p.startsWith("`") && p.endsWith("`") && p.length > 2) {
          return (
            <code key={i} className="rounded bg-[var(--color-muted)] px-1 py-0.5 font-mono text-[0.92em] [overflow-wrap:anywhere]">
              {p.slice(1, -1)}
            </code>
          );
        }
        if (p.startsWith("*") && p.endsWith("*") && p.length > 2) return <em key={i}>{p.slice(1, -1)}</em>;
        return <Fragment key={i}>{p}</Fragment>;
      })}
    </>
  );
}

const TONE: Record<string, string> = {
  neutral: "border-[var(--color-border)] bg-[var(--color-muted)]/40",
  bad: "border-red-500/40 bg-red-500/10",
  warn: "border-amber-500/40 bg-amber-500/10",
  good: "border-emerald-500/40 bg-emerald-500/10",
};

export function ReportView({ blocks }: { blocks: ReportBlock[] }) {
  return (
    <div className="space-y-3 text-sm" data-testid="onec-report">
      {blocks.map((b, i) => {
        switch (b.type) {
          case "title":
            return (
              <header key={i} className="space-y-1 border-b border-[var(--color-border)] pb-3">
                <h2 className="text-lg font-semibold [overflow-wrap:anywhere]">{b.text}</h2>
                {b.meta.map(([k, v]) => (
                  <p key={k} className="text-xs text-[var(--color-muted-foreground)]">
                    <span className="font-medium">{k}</span> {v}
                  </p>
                ))}
              </header>
            );
          case "heading":
            return b.level <= 2
              ? <h3 key={i} className="pt-3 text-base font-semibold">{b.text}</h3>
              : <h4 key={i} className="pt-1 text-sm font-semibold">{b.text}</h4>;
          case "para":
            if (b.kind === "callout") {
              return (
                <div key={i} className={cn("rounded-md border px-3 py-2 leading-relaxed", TONE[b.tone] ?? TONE.neutral)}>
                  <Inline text={b.text} />
                </div>
              );
            }
            return (
              <p key={i} className={cn("leading-relaxed", b.kind === "note" && "text-xs text-[var(--color-muted-foreground)]")}>
                <Inline text={b.text} />
              </p>
            );
          case "bullets": {
            const List = b.ordered ? "ol" : "ul";
            return (
              <List key={i} className={cn("space-y-1 pl-5", b.ordered ? "list-decimal" : "list-disc")}>
                {b.items.map((it, j) => <li key={j}><Inline text={it} /></li>)}
              </List>
            );
          }
          case "table":
            return (
              <div key={i} className="overflow-x-auto rounded-md border border-[var(--color-border)]">
                <table className="w-full text-left text-xs">
                  <thead className="bg-[var(--color-muted)]/60">
                    <tr>{b.header.map((h, j) => <th key={j} className="px-2 py-1.5 font-medium">{h}</th>)}</tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j} className="border-t border-[var(--color-border)] align-top">
                        {r.map((c, k) => (
                          <td key={k} className={cn("px-2 py-1.5 [overflow-wrap:anywhere]", b.mono.includes(k) && "font-mono")}>
                            {b.mono.includes(k) ? c : <Inline text={c} />}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "stats":
            return (
              <div key={i} className="flex flex-wrap gap-2">
                {b.items.map((s, j) => (
                  <div key={j} className="rounded-md border border-[var(--color-border)] px-3 py-1.5">
                    <div className="text-base font-semibold">{s.value}</div>
                    <div className="text-xs text-[var(--color-muted-foreground)]">{s.label}</div>
                  </div>
                ))}
              </div>
            );
          case "code":
            return (
              <pre key={i} className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-2 font-mono text-[12px] leading-relaxed">
                {b.lines.join("\n")}
              </pre>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
