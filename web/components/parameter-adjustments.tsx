"use client";

/**
 * What Celmis changed behind the operator's back on a review run, and what to
 * do about it.
 *
 * THE PRINCIPLE. The runtime self-heals in four places: a ceiling above the
 * model's maximum is clamped, a reasoning level the provider refuses is
 * dropped and the call retried, a temperature the model refuses is dropped the
 * same way (claude-sonnet-5 takes only 1, and Celmis sent 0.1 for every agent
 * — the architect agent, 79% of one installation's true positives, ran zero
 * times until that retry existed), and a fallback model steps in for an agent
 * the primary could not serve. Every one of those is the right call in the
 * moment: a review that ran without the knob is worth more than no review.
 *
 * And every one of them was invisible. Each was recorded in a different place
 * — a field on the LLM result, a process-wide memory, an audit record and a
 * log line, a flag on the agent result — and none of it reached a screen. So a
 * review quietly got worse (its reasoning dropped on every run from the second
 * one onward) and nobody knew which knob to turn, or that there was a knob.
 *
 * This file is the one place the four are read. Each row says what was asked,
 * what was sent, why, and — the part that makes it actionable — where the fix
 * lives. A parameter changed behind the operator's back must be shown with a
 * remedy attached, or the operator learns about it from a worse review.
 *
 * Defensive about the wire on purpose: the list and its count are new, older
 * runs carry neither, and the vocabulary (`parameter`, `action`) is open so a
 * fifth self-heal renders as a row before this file learns its name.
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  reviewRunsApi, type ParameterAdjustment, type ReviewRunOut,
} from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/** The rows a run carries, or null when it carries none — which for a run
 *  recorded before the list existed is "nobody wrote it down", not "nothing
 *  was changed", so a consumer must not read null as an all-clear. */
export function adjustmentsOf(
  run: Pick<ReviewRunOut, "parameter_adjustments"> | null | undefined,
): ParameterAdjustment[] | null {
  const list = run?.parameter_adjustments;
  return Array.isArray(list) ? list : null;
}

/** How many adjustments to announce for a row.
 *
 *  Either field alone is enough: a lean history may ship the count without
 *  the rows, a detail fetch ships the rows without bothering with a count.
 *  When both arrive the larger wins — a count above the rows it came with
 *  means the rows were trimmed, and the badge must still say the true
 *  number so the operator opens it and the panel fetches the rest. A run
 *  with neither is an older run: zero, and no badge, because a badge over
 *  "nobody wrote it down" would be a claim.
 */
export function adjustmentsCount(
  run: Pick<ReviewRunOut, "parameter_adjustments" | "adjustments_count"> | null | undefined,
): number {
  const rows = adjustmentsOf(run)?.length ?? 0;
  const n = run?.adjustments_count;
  const counted = typeof n === "number" && Number.isFinite(n) && n > 0 ? n : 0;
  return Math.max(rows, counted);
}

/** Which fix a row points at, and where that fix lives.
 *
 *  `kind` picks the sentence; `links` the pages it sends the operator to.
 *  "temperatureDropped" has no link on purpose: there is no temperature
 *  control anywhere, the model accepts only its own default, and the honest
 *  remedy is "nothing — noted for comparability". Sending the operator to a
 *  page with nothing to change on it would be the dead end this whole
 *  surface exists to end.
 */
export type AdjustmentRemedy = {
  kind:
    | "clamp" | "reasoningDropped" | "temperatureDropped" | "swap"
    | "graphMissing" | "graphBaseTooOld" | "graphNoParser" | "other";
  links: Array<{ label: "agents" | "fallback" | "repositories"; href: string }>;
};

/** The remedy for one adjustment.
 *
 *  Keyed on the PARAMETER first and the action second: the parameter says
 *  what the operator can change, the action only says what the runtime did.
 *  A clamped ceiling, a dropped reasoning level and a swapped model are
 *  fixed on three different controls; the same action on an unknown
 *  parameter falls back to the action's general remedy rather than to
 *  nothing, and an unknown pair still names the per-agent page — the one
 *  place every knob this runtime honours is set.
 *
 *  The two hrefs are ids on /settings/llm: the fallback model sits on the
 *  review profile card, the per-agent ceiling and reasoning level one card
 *  below it. Anchors, so the link lands on the control and not at the top
 *  of a long page. Spelled here rather than in a module constant so the
 *  function is one self-contained declaration the test harness can lift.
 */
export function adjustmentRemedy(
  adj: Pick<ParameterAdjustment, "parameter" | "action">,
): AdjustmentRemedy {
  const parameter = String(adj.parameter ?? "").trim().toLowerCase();
  const action = String(adj.action ?? "").trim().toLowerCase();
  const agents = { label: "agents" as const, href: "/settings/llm#review-agents" };
  const fallback = { label: "fallback" as const, href: "/settings/llm#review-fallback" };
  const repositories = { label: "repositories" as const, href: "/repositories" };
  if (parameter === "max_output_tokens") return { kind: "clamp", links: [agents] };
  // Not a model parameter at all: the code graph was missing or stale and
  // the agents reviewed the diff without its blast radius. The fix is not on
  // the LLM page — it is indexing the repository, so the link goes there.
  //
  // Except when it is not a fix. "base_too_old" means every changed file the
  // index lacks is also absent from the checkout the index was built from:
  // the pull request's base is older than the indexed revision and those
  // files were renamed or deleted before it. One 2023 benchmark PR was
  // missing 51 of its 172 changed files that way, and the note told the
  // operator to re-index — which could not have added a single one of them.
  // So that row gets no link at all, like a dropped temperature: there is
  // nothing to change, and a link to a page that changes nothing is the dead
  // end this column exists to end.
  //
  // And a third case that is not a fix either, for a different reason.
  // "unsupported_language" means the changed files are written in a language
  // Celmis has no extractor for: the repository can be perfectly indexed and
  // still hold nothing for them. Measured on discourse — 8185 .rb files and
  // zero Ruby symbols in the graph. Re-indexing runs the same extractors and
  // finds the same nothing, so this row gets no link either.
  if (parameter === "graph_context") {
    if (action === "base_too_old") return { kind: "graphBaseTooOld", links: [] };
    if (action === "unsupported_language") return { kind: "graphNoParser", links: [] };
    return { kind: "graphMissing", links: [repositories] };
  }
  if (parameter === "model") return { kind: "swap", links: [fallback] };
  if (parameter === "temperature") return { kind: "temperatureDropped", links: [] };
  // "pick another level on the LLM page, or set a fallback model" — two
  // remedies, two links, because a level the provider refuses is fixed either
  // by choosing a level it takes or by letting a different model answer.
  if (parameter === "reasoning") return { kind: "reasoningDropped", links: [agents, fallback] };
  if (action === "clamped") return { kind: "clamp", links: [agents] };
  if (action === "swapped") return { kind: "swap", links: [fallback] };
  return { kind: "other", links: [agents] };
}

/** The parameters and actions this file has words for. Anything else is
 *  rendered raw rather than through a key that does not exist — `t()` falls
 *  back to the key id, and "reviews.adjParam.foo" in a cell is worse than
 *  "foo". */
const KNOWN_PARAMETERS = new Set([
  "max_output_tokens", "reasoning", "temperature", "model", "graph_context",
]);
const KNOWN_ACTIONS = new Set([
  "clamped", "dropped", "swapped", "unavailable", "partial", "base_too_old",
  "unsupported_language",
]);

/** A requested/sent value as a cell. Null is a dash; for a dropped parameter
 *  the "sent" side is not a value at all, it is the absence of one, and the
 *  cell says so in words rather than leaving a dash the eye reads as
 *  "unknown". */
function cellValue(
  v: string | number | null | undefined,
  t: (key: string) => string,
  opts?: { dropped?: boolean },
): string {
  if (v === null || v === undefined || v === "") {
    return opts?.dropped ? t("reviews.adjNotSent") : "—";
  }
  return String(v);
}

/**
 * The table for one run: agent · parameter · asked · sent · why, each row
 * followed by what to do about it.
 *
 * Fetches the run on demand when the row it was handed carries fewer
 * adjustments than it counts — a lean history — and otherwise renders what it
 * was given without a request.
 */
export function ParameterAdjustmentsPanel({ run }: { run: ReviewRunOut }) {
  const token = useToken();
  const t = useT();
  const own = adjustmentsOf(run);
  const count = adjustmentsCount(run);
  // Rows arrived with the history entry: nothing to fetch. The count is the
  // truth the badge showed; when the rows fall short of it, the row was
  // trimmed and the detail endpoint has the rest.
  const needFetch = (own?.length ?? 0) < count;
  const detail = useQuery({
    queryKey: ["review-run", run.id],
    queryFn: () => reviewRunsApi.get(token!, run.id),
    enabled: !!token && needFetch && !!run.id,
    staleTime: 60_000,
  });
  const rows = needFetch ? adjustmentsOf(detail.data) : own;

  if (!rows) {
    // Either still loading, or the server answered without the list — an
    // older server behind a newer page. Say which; a blank panel under a
    // badge that promised rows is the bug this surface exists to end.
    const failed = detail.isError || (detail.isSuccess && !adjustmentsOf(detail.data));
    return (
      <p className="mt-2 text-xs text-[var(--color-muted-foreground)]">
        {failed ? t("reviews.adjustmentsLoadFailed") : t("reviews.adjustmentsLoading")}
      </p>
    );
  }

  return (
    <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-secondary)]/60 p-2">
      <p className="mb-1 text-xs font-medium">{t("reviews.adjustmentsTitle")}</p>
      <p className="mb-2 text-[11px] text-[var(--color-muted-foreground)]">
        {t("reviews.adjustmentsIntro")}
      </p>
      <Table>
        <THead>
          <TR>
            <TH>{t("reviews.adjCol.agent")}</TH>
            <TH>{t("reviews.adjCol.parameter")}</TH>
            <TH>{t("reviews.adjCol.requested")}</TH>
            <TH>{t("reviews.adjCol.sent")}</TH>
            <TH>{t("reviews.adjCol.reason")}</TH>
          </TR>
        </THead>
        <TBody>
          {rows.map((adj, i) => {
            const parameter = String(adj.parameter ?? "");
            const action = String(adj.action ?? "");
            const remedy = adjustmentRemedy(adj);
            const dropped = action.toLowerCase() === "dropped";
            return (
              <TR key={`${adj.agent ?? ""}-${parameter}-${i}`}>
                <TD className="whitespace-nowrap">
                  <span className="capitalize">{adj.agent || "—"}</span>
                  {/* WHO refused or clamped it. "reasoning 'minimal' was
                      dropped" is only actionable together with the model
                      that would not take it — it is the model the operator
                      goes looking for on the LLM page. */}
                  {adj.model && (
                    <span className="block font-mono text-[10px] text-[var(--color-muted-foreground)]">
                      {adj.model}
                    </span>
                  )}
                </TD>
                <TD className="whitespace-nowrap">
                  {KNOWN_PARAMETERS.has(parameter.toLowerCase())
                    ? t(`reviews.adjParam.${parameter.toLowerCase()}`)
                    : parameter || "—"}
                  <span className="ml-1 text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
                    {KNOWN_ACTIONS.has(action.toLowerCase())
                      ? t(`reviews.adjAction.${action.toLowerCase()}`)
                      : action}
                  </span>
                </TD>
                <TD className="whitespace-nowrap font-mono">{cellValue(adj.requested, t)}</TD>
                <TD className={`whitespace-nowrap font-mono ${dropped ? "text-[var(--color-warning)]" : ""}`}>
                  {cellValue(adj.sent, t, { dropped })}
                </TD>
                <TD className="min-w-[16rem]">
                  <div className="wrap-anywhere">{adj.reason || "—"}</div>
                  {/* The remedy, in the row it belongs to. Not a footnote
                      under the table: an operator reading one agent's row
                      should not have to match a list of fixes back to it. */}
                  <div className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">
                    {"→ "}{t(`reviews.adjRemedy.${remedy.kind}`)}
                    {remedy.links.map((l) => (
                      <span key={l.label}>
                        {" "}
                        <Link className="underline" href={l.href}>
                          {t(`reviews.adjRemedyLink.${l.label}`)}
                        </Link>
                      </span>
                    ))}
                  </div>
                </TD>
              </TR>
            );
          })}
        </TBody>
      </Table>
    </div>
  );
}
