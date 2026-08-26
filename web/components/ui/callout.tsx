import { cn } from "@/lib/utils";

export type CalloutTone = "info" | "warning" | "danger" | "success";

/** Tone → border/background/text classes, matching the hand-rolled banners
 * used across the app (amber/red/emerald/blue on translucent tint). */
const TONES: Record<CalloutTone, string> = {
  info: "border-blue-500/40 bg-blue-500/10 text-blue-600 dark:text-blue-400",
  warning: "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400",
  danger: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400",
  success: "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
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
    <div className={cn("rounded-md border px-3 py-2 text-xs", TONES[tone], className)}>
      {children}
    </div>
  );
}
