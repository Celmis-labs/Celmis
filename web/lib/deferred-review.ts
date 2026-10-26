/**
 * A review that waits for its model.
 *
 * The server parks the run as `deferred` and says why (`waiting.reason`, a
 * curated sentence), since when and when the model is looked at next. "Retry
 * now" is the same endpoint the jobs page uses and is for workspace
 * owners/admins only — the API decides, this only decides what to draw.
 */
import type { ReviewRunWaiting } from "@/lib/api";

export function isDeferred(status: string | null | undefined): boolean {
  return status === "deferred";
}

/** Retry-now needs a job to release and somebody allowed to release it.
 *  `canManage` is undefined while the membership loads: nothing is drawn yet. */
export function canRetryNow(
  waiting: ReviewRunWaiting | null | undefined,
  canManage: boolean | undefined,
): boolean {
  return Boolean(waiting?.job_id) && canManage === true;
}

/** "Sentence." — the curated reason without a doubled full stop. */
export function reasonSentence(reason: string | null | undefined): string {
  const r = (reason ?? "").trim().replace(/\.+$/, "");
  return r ? `${r[0].toUpperCase()}${r.slice(1)}.` : "";
}
