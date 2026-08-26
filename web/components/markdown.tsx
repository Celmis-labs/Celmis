"use client";

/**
 * Themed markdown renderer for assistant answers (Q&A chat).
 *
 * Built on react-markdown + remark-gfm with a component map wired to the
 * app's theme tokens. Inline code that looks like a file citation
 * (`repo/path/file.ts:123` or `file.ts#L12`) renders as a button that jumps
 * to /search pre-filled with the file's basename — the search page already
 * resolves files to provider links.
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useRouter } from "next/navigation";

import { cn } from "@/lib/utils";

/** `repo/path/to/file.ts:123`, `path.py#L12`, `web/lib/api.ts` … */
const FILE_CITE_RE =
  /^[\w./-]+\.(ts|tsx|py|go|rs|js|jsx|md|json|yml|yaml|sql|css)(#L\d+|:\d+)?$/;

export function Markdown({ text, className }: { text: string; className?: string }) {
  const router = useRouter();

  const openCitation = (raw: string) => {
    const withoutLine = raw.replace(/(#L\d+|:\d+)$/, "");
    const basename = withoutLine.split("/").pop() ?? withoutLine;
    router.push(`/search?q=${encodeURIComponent(basename)}`);
  };

  return (
    <div className={cn("space-y-2 text-sm leading-relaxed break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-[var(--color-brand)] underline underline-offset-2 hover:opacity-80"
            >
              {children}
            </a>
          ),
          code: ({ className: codeClass, children }) => {
            const content = String(children ?? "");
            // Block code (fenced) carries a language-* class or newlines;
            // everything else is inline.
            const isBlock = /language-/.test(codeClass ?? "") || content.includes("\n");
            if (!isBlock && FILE_CITE_RE.test(content.trim())) {
              return (
                <button
                  type="button"
                  onClick={() => openCitation(content.trim())}
                  className="inline break-all rounded bg-[var(--color-muted)] px-1 py-0.5 font-mono text-[0.85em] text-[var(--color-brand)] underline underline-offset-2 hover:opacity-80"
                >
                  {content.trim()}
                </button>
              );
            }
            return (
              <code
                className={cn(
                  "rounded bg-[var(--color-muted)] px-1 py-0.5 font-mono text-[0.85em]",
                  codeClass,
                )}
              >
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            <pre className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)] p-3 text-xs leading-relaxed [&_code]:bg-transparent [&_code]:p-0">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            // The wrapper could scroll, but `w-full` meant the table never
            // asked to be wider than it: on a phone a ten-column table
            // squeezed every cell to about one character instead. min-w-max
            // lets it take its natural width so the wrapper actually scrolls,
            // and `w-full` stays as the floor so a two-column table still
            // fills the line on a desktop.
            <div className="w-full overflow-x-auto overscroll-x-contain">
              <table className="w-full min-w-max border-collapse text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="whitespace-nowrap border-b border-[var(--color-border)] px-2 py-1 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-[var(--color-border)]/50 px-2 py-1 align-top [overflow-wrap:normal]">
              {children}
            </td>
          ),
          ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-[var(--color-border)] pl-3 text-[var(--color-muted-foreground)]">
              {children}
            </blockquote>
          ),
          h1: ({ children }) => <h1 className="text-base font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="text-sm font-semibold">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-semibold">{children}</h3>,
          h4: ({ children }) => <h4 className="text-sm font-medium">{children}</h4>,
          hr: () => <hr className="border-[var(--color-border)]" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
