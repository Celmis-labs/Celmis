"use client";

/**
 * Copy-to-clipboard for the Codebase pages: an invite link, an install
 * command, the brief an agent will receive.
 *
 * `copyText` (lib/copy.ts) rather than `navigator.clipboard`: this product is
 * often served over plain HTTP on an internal address, where the Clipboard API
 * does not exist and a bare `writeText` throws before it can say so. The
 * button reports the outcome either way.
 */

import { useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { toast } from "sonner";

import { copyText } from "@/lib/copy";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export function CopyButton({
  text,
  label,
  size = "sm",
  className,
}: {
  text: string;
  /** Button text; defaults to "Copy". */
  label?: string;
  size?: "xs" | "sm";
  className?: string;
}) {
  const t = useT();
  const [done, setDone] = useState(false);
  return (
    <Button
      type="button"
      size={size}
      variant="outline"
      className={className}
      onClick={async () => {
        if (await copyText(text)) {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } else {
          toast.error(t("codebase.copyFailed"));
        }
      }}
    >
      {done ? <CheckIcon /> : <CopyIcon />}
      {done ? t("codebase.copied") : (label ?? t("codebase.copy"))}
    </Button>
  );
}

/** Text to copy, shown as it will be pasted. `wrap` keeps long prose (a brief)
 *  readable; commands scroll instead, because a wrapped command is a command
 *  someone retypes wrongly. */
export function CodeBlock({
  text,
  label,
  wrap = false,
  className,
}: {
  text: string;
  label?: string;
  wrap?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <pre
        className={cn(
          "rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3 text-[12px] leading-relaxed",
          wrap ? "max-h-[28rem] overflow-auto whitespace-pre-wrap break-words" : "overflow-x-auto",
        )}
      >
        <code>{text}</code>
      </pre>
      <div className="flex justify-end">
        <CopyButton text={text} label={label} />
      </div>
    </div>
  );
}
