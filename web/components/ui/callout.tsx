import { cn } from "@/lib/utils";

export type CalloutTone = "info" | "warning" | "danger" | "success";

/** Tone → the semantic tokens (text on its own tint, ≥4.5:1 in both themes).
 *  These were raw blue/amber/red/emerald classes: the one "standard" banner
 *  that bypassed the colour system. */
const TONES: Record<CalloutTone, string> = {
  info: "border-[var(--color-info)]/30 bg-[var(--color-info-soft)] text-[var(--color-info)]",
  warning: "border-[var(--color-warning)]/35 bg-[var(--color-warning-soft)] text-[var(--color-warning)]",
  danger: "border-[var(--color-destructive)]/35 bg-[var(--color-destructive-soft)] text-[var(--color-destructive)]",
  success: "border-[var(--color-success)]/30 bg-[var(--color-success-soft)] text-[var(--color-success)]",
};

/** Standardized inline banner. */
export function Callout({
  tone = "info",
  className,
  children,
}: {
  tone?: CalloutTone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border px-3 py-2 text-xs leading-relaxed", TONES[tone], className)}>
      {children}
    </div>
  );
}
