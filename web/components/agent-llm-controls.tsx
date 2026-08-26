"use client";

/**
 * The per-agent LLM controls — model, output ceiling, reasoning — and the
 * decisions behind them, in the one file both screens that set them render.
 *
 * WHY ONE FILE. These controls were born on /settings/llm, where they are the
 * WORKSPACE default. The layer that actually WINS is one below: a repository's
 * review policy, on /admin/review-policies/<slug>. That screen had five model
 * dropdowns and nothing else — no ceiling, no reasoning, and no hint that
 * either existed anywhere. So the screen with the most authority showed the
 * least: an operator who pointed an agent at a model there could not see that
 * the model refuses the reasoning value stored a layer up, nor that a layer up
 * was where it lived.
 *
 * Copying the row into the policy page would have made the two diverge; the
 * last copy this codebase kept (a vendor-prefix helper written twice, once in
 * Python and once in TypeScript) is exactly why capabilities are a server call
 * and not a table. So the row moved here and both screens render this one.
 *
 * The two layers differ in one thing a person can see: what an empty box
 * inherits from. `inheritsFrom` says which, and it is the only reason this
 * component knows it has more than one caller.
 */

import Link from "next/link";
import { useQueries } from "@tanstack/react-query";

import {
  llmApi,
  AGENT_TOKENS_MAX, AGENT_TOKENS_MIN,
  type AgentLLMOverride, type AgentSettings, type ModelCapabilities,
  type ProviderRefusal,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDate } from "@/lib/format";
import { ModelSelect } from "@/components/model-select";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

/* THERE IS NO VENDOR-PREFIX HELPER IN THIS FILE, ON PURPOSE.
 *
 * There used to be one — `withVendorPrefix(model, reviewProvider)` — and it
 * welded the REVIEW PROFILE's vendor onto whatever bare id the row carried.
 * An operator on a Gemini review profile who pointed one agent at "gpt-4o"
 * got "gemini/gpt-4o" asked about, which litellm has no entry for: the row
 * said "unrecognised model", hid the 16384 ceiling and disabled the reasoning
 * control, for a model litellm knows perfectly well and that the runtime
 * routes to OpenAI with the OpenAI key. The screen contradicted the runtime,
 * and the screen was the one that was wrong.
 *
 * A bare id is not this file's to resolve. GET /model-capabilities answers a
 * bare "gpt-4o" exactly as it answers "openai/gpt-4o", because litellm
 * resolves the vendor itself — so the string goes over the wire untouched and
 * the one resolver that exists lives on the server, where the runtime reads
 * it too. Re-deriving it here is how the two got to disagree in the first
 * place, and a SECOND screen re-deriving it is how they would again.
 */

/** What `ReviewSettings.agent_max_output_tokens` ships as. Named where an
 *  operator reads the number this screen exists to explain; if that default
 *  moves in src/review/settings.py, move this with it. */
export const DEFAULT_AGENT_MAX_OUTPUT = 16384;

/** The form for one agent. Everything is a string here — the empty string is
 *  "inherit", which is the one value a number input cannot hold. */
export type AgentDraft = {
  model: string; maxOut: string; reasoning: string; temperature: string;
};

/** One stored override as the three form fields. Both screens map their own
 *  agent list through this, so "empty string means inherit" is spelled once. */
export function agentDraftFrom(
  entry: AgentLLMOverride | null | undefined,
): AgentDraft {
  return {
    model: entry?.model ?? "",
    maxOut: entry?.max_output_tokens != null ? String(entry.max_output_tokens) : "",
    reasoning: entry?.reasoning != null ? String(entry.reasoning) : "",
    // != null, not truthiness: 0 is the most deterministic temperature
    // there is and must render as a value, never as "inherit".
    temperature: entry?.temperature != null ? String(entry.temperature) : "",
  };
}

/** The typed reasoning value in the shape the chosen model's vendor takes.
 *
 *  "Reasoning effort" is not one vocabulary: OpenAI takes a word, Anthropic
 *  and Gemini a thinking budget in tokens. `reasoning_kind` is what decides,
 *  and it comes from litellm rather than from a table in this file. A budget
 *  that will not parse is sent back verbatim rather than as NaN — NaN
 *  serialises to null, which would read as "inherit" and hide the typo.
 */
export function reasoningValue(
  raw: string, caps: ModelCapabilities | null,
): string | number | null {
  const v = raw.trim();
  if (!v) return null;
  if (caps?.reasoning_kind === "budget") {
    const n = Number(v);
    return Number.isFinite(n) ? n : v;
  }
  return v;
}

/** Whether this model can be sent a reasoning setting at all.
 *
 *  Not "does litellm say it reasons" — `reasoning_kind` is the narrower and
 *  correct question, because it is null both for a model that cannot think
 *  and for one litellm has never heard of. In both cases the parameter would
 *  be dropped before the request left, so the server refuses to store it, and
 *  a control that accepted one here would only be collecting a 422.
 *
 *  An effort model whose vocabulary this build cannot name is the same story
 *  with a different cause: the server accepts only a word from that list, so
 *  an empty list means every word is a 422 and the dropdown would be a row of
 *  choices that all fail.
 */
export function takesReasoning(caps: ModelCapabilities | null): boolean {
  if (!caps || caps.reasoning_kind === null) return false;
  if (caps.reasoning_kind === "effort") return Boolean(caps.reasoning_values?.length);
  return true;
}

/** The reasoning override this layer currently holds for the agent, or null.
 *  Blank is folded into null: the server strips blanks before storing, so a
 *  blank here could only ever mean "nothing is set". */
export function storedReasoning(
  entry: AgentLLMOverride | undefined | null,
): string | number | null {
  const v = entry?.reasoning;
  if (v === undefined || v === null) return null;
  return typeof v === "string" && !v.trim() ? null : v;
}

/** What the provider has refused for this model, for one parameter.
 *
 *  THE BUG THIS ANSWERS. A reasoning word the provider answered 400 to was
 *  struck off `reasoning_values` by the server — correctly, the runtime stops
 *  sending it — and the dropdown simply stopped listing it. No reason, no
 *  date, no remedy: an operator who had saved "minimal" came back to a row
 *  whose control no longer had a "minimal" in it and a review that had quietly
 *  been running without it since. The refusal is a FACT the server learned
 *  from a real call, and a fact with a date is something a screen can say.
 *
 *  Two wire shapes, one answer. `provider_refusals` is the contract: entries
 *  with the provider's sentence and when it was learned, any parameter. The
 *  older `reasoning_values_provider_refused` is the bare word list that
 *  crossed the boundary first; it is folded in (reason empty, date null) so a
 *  page ahead of its server still strikes the word instead of losing it, and
 *  unioned rather than ignored when both arrive, because a word either list
 *  names is a word the provider refused. Null caps is "did not get to ask",
 *  which is no refusals — not a clean bill, and nothing here claims one.
 */
export function providerRefusals(
  caps: ModelCapabilities | null, parameter: string,
): ProviderRefusal[] {
  if (!caps) return [];
  const want = String(parameter ?? "").trim().toLowerCase();
  const out: ProviderRefusal[] = [];
  const seen = new Set<string>();
  const keyOf = (v: string | number | null | undefined) => String(v ?? "").trim().toLowerCase();
  for (const r of Array.isArray(caps.provider_refusals) ? caps.provider_refusals : []) {
    if (!r || String(r.parameter ?? "").trim().toLowerCase() !== want) continue;
    out.push(r);
    seen.add(keyOf(r.value));
  }
  if (want === "reasoning" && Array.isArray(caps.reasoning_values_provider_refused)) {
    for (const word of caps.reasoning_values_provider_refused) {
      if (typeof word !== "string" || !word.trim() || seen.has(keyOf(word))) continue;
      out.push({ parameter: "reasoning", value: word, reason: "", seen_at: null });
      seen.add(keyOf(word));
    }
  }
  return out;
}

/** The refusal on file for one VALUE of one parameter, or null.
 *
 *  Compared as lower-cased trimmed text, because the operator's saved word
 *  and the word the provider refused are the same word however either was
 *  typed, and a budget in tokens is compared the same way — "4096" and 4096
 *  are one value. Null and blank are "nothing set", which nothing can refuse.
 */
export function refusalFor(
  value: string | number | null | undefined,
  caps: ModelCapabilities | null,
  parameter: string,
): ProviderRefusal | null {
  if (value === null || value === undefined) return null;
  const word = String(value).trim().toLowerCase();
  if (!word) return null;
  return providerRefusals(caps, parameter).find(
    (r) => String(r.value ?? "").trim().toLowerCase() === word,
  ) ?? null;
}

/** The provider's word that this model takes only its own default
 *  temperature, or null when nothing of the kind has been learned.
 *
 *  There is no temperature control on any screen — Celmis sends one value to
 *  every review agent — so this is the one place the operator can learn it
 *  rather than from a badge on a run. The first entry is enough: a second
 *  refused value would only restate the same fact. */
export function temperatureFixedToDefault(
  caps: ModelCapabilities | null,
): ProviderRefusal | null {
  return providerRefusals(caps, "temperature")[0] ?? null;
}

/** What one agent's `reasoning` field should be on the way out — or
 *  `undefined` for "omit it".
 *
 *  READ THE NEXT SENTENCE BEFORE CHANGING ANYTHING HERE. Both layers REPLACE
 *  their stored map rather than patching it (neither has another way to
 *  express "stop overriding"), so omitting this field DELETES whatever was
 *  stored. `undefined` is not "leave it alone"; it is "erase it".
 *
 *  That is how this went wrong. The rule used to be a bare
 *  `if (takesReasoning(caps))`, and `caps` is null in three states that have
 *  nothing to do with the model's abilities: the lookup in flight, the lookup
 *  errored (`retry: false` makes one blip final for the page's lifetime), and
 *  no model to ask about. Pressing Save in any of them wiped every affected
 *  agent's reasoning override — a configured value that quietly stopped
 *  existing, which is the exact failure `gemini_thinking_budget` was and the
 *  exact failure this whole surface was built to end.
 *
 *  So the destructive branch is the narrow one: we drop a stored value only
 *  when the lookup ANSWERED and the answer names no reasoning parameter — the
 *  case where the server would 422 it back anyway, and where the row says out
 *  loud that saving removes it. An unanswered lookup keeps what was loaded,
 *  because "we could not check" is not evidence of anything.
 *
 *  Not a merge instruction to the server (option b) — a partial PUT could then
 *  never clear an override, since absent is the only spelling of "inherit"
 *  this chain has. Not a blocked Save either (option c): an operator locked
 *  out of saving because one lookup blipped is worse off than one whose
 *  untouched setting simply survived. Sending back what was loaded is the
 *  only shape where the value the operator never touched is the value that is
 *  still there afterwards.
 */
export function reasoningToSave(
  draft: AgentDraft,
  stored: AgentLLMOverride | undefined | null,
  caps: ModelCapabilities | null,
): string | number | undefined {
  if (takesReasoning(caps)) {
    // The row was editable, so the box is the answer — empty included, which
    // is how the operator says "stop overriding this".
    const r = reasoningValue(draft.reasoning, caps);
    return r === null ? undefined : r;
  }
  if (caps !== null) {
    // Answered, and the answer is no: LiteLLM names no parameter for this
    // model, so the server refuses to store one. Dropping it is the only
    // thing that can be saved — and the row warns before the press.
    return undefined;
  }
  // Unanswered. Hand back exactly what was loaded; `?? undefined` only when
  // there was nothing stored to begin with.
  return storedReasoning(stored) ?? undefined;
}

/** One agent's whole override entry on the way out, or null for "no override"
 *  — which is how both layers spell "inherit".
 *
 *  `withModel` is false for the repo policy, whose model has lived in its own
 *  flat `<agent>_model` column since Stage 11. Writing it into the entry as
 *  well would give one setting two spellings in one payload, and the resolver
 *  reads the column first — so the copy in the entry would be the one that
 *  looks authoritative and never applies.
 */
export function agentEntryToSave(
  draft: AgentDraft,
  stored: AgentLLMOverride | undefined | null,
  caps: ModelCapabilities | null,
  opts?: { withModel?: boolean },
): AgentLLMOverride | null {
  const entry: AgentLLMOverride = {};
  if ((opts?.withModel ?? true) && draft.model.trim()) entry.model = draft.model.trim();
  if (draft.maxOut.trim()) entry.max_output_tokens = Number(draft.maxOut.trim());
  if ((draft.temperature ?? "").trim()) entry.temperature = Number((draft.temperature ?? "").trim());
  const r = reasoningToSave(draft, stored, caps);
  if (r !== undefined) entry.reasoning = r;
  // An empty entry is not "set to nothing", it is "no override" — and absent
  // is how the inheritance chain spells that.
  return Object.keys(entry).length > 0 ? entry : null;
}

/** One row's answer from GET /model-capabilities, plus why it is missing.
 *
 *  `caps: null` is NOT "this model cannot do that" — it is "we did not get to
 *  ask, or the asking failed". Three states land there: in flight, errored
 *  (the query sets `retry: false`, so one blip is final for as long as the
 *  page is open), and disabled because the row has no model to ask about.
 *  Every caller that turns an answer into a destructive decision has to tell
 *  the two apart; see `reasoningToSave`.
 */
export type AgentCaps = {
  caps: ModelCapabilities | null;
  loading: boolean;
  failed: boolean;
};

/**
 * The capabilities of the models a page's agent rows are pointed at, in order.
 *
 * One hook at CARD level rather than a query inside each row, in both callers:
 * the Save button has to refuse a value above a ceiling BEFORE the row that
 * knows the ceiling has finished rendering, so the ceilings have to live where
 * the button lives.
 *
 * The strings go over the wire untouched — see the note at the top of this
 * file. Callers pass whatever the chain resolved to, bare id or prefixed.
 */
export function useAgentCapabilities(models: string[]): AgentCaps[] {
  const token = useToken();
  const results = useQueries({
    queries: models.map((model) => ({
      queryKey: ["model-capabilities", model],
      queryFn: () => llmApi.modelCapabilities(token!, model),
      enabled: !!token && !!model,
      staleTime: 10 * 60_000,
      // A model litellm has no entry for is an ANSWER, not a blip. Retrying
      // only delays the "unknown" the operator is waiting to read.
      retry: false,
    })),
  });
  return results.map((r) => ({
    caps: r.data ?? null,
    // `fetchStatus`, not `isPending`: a disabled query (no model to ask about)
    // is pending forever, and "loading…" would never stop.
    loading: r.fetchStatus === "fetching",
    failed: r.isError,
  }));
}

/** The model's own ceiling, or null when nobody can vouch for one. */
export function agentCeiling(caps: ModelCapabilities | null): number | null {
  return caps && caps.known ? caps.max_output_tokens : null;
}

/** What the form will let through: the narrower of the model's ceiling and
 *  the server's own bound. Never wider than the server accepts. */
export function agentMaxOutLimit(caps: ModelCapabilities | null): number {
  return Math.min(agentCeiling(caps) ?? AGENT_TOKENS_MAX, AGENT_TOKENS_MAX);
}

/** Refused in the form, so the server never has to 422 it. */
export function agentMaxOutError(
  raw: string, caps: ModelCapabilities | null,
): "range" | "over" | null {
  const v = raw.trim();
  if (!v) return null;
  const n = Number(v);
  if (!Number.isInteger(n) || n < AGENT_TOKENS_MIN || n > AGENT_TOKENS_MAX) return "range";
  const ceiling = agentCeiling(caps);
  return ceiling != null && n > ceiling ? "over" : null;
}

/** Which layer answers when a box here is left empty.
 *
 *  "profile" — /settings/llm, where the next layer down is the review surface
 *  profile chosen on the same page and there is nowhere else to send anybody.
 *  "workspace" — a repo policy, where the next layer down is a whole other
 *  screen. An operator standing on the layer with the most authority could
 *  not previously tell that screen existed, so the row links to it.
 */
export type AgentInheritsFrom = "profile" | "workspace";

/** One agent's three controls, plus what the model behind them can do.
 *
 *  Deliberately hook-free apart from `useT`: the capabilities live in the card
 *  (see `useAgentCapabilities`), so this renders whatever it is handed and
 *  nothing in it can change hook order.
 */
export function AgentLLMRow({
  agent, draft, stored, model, caps, loading, failed, limit, error, disabled,
  inheritsFrom = "profile", effective, inheritedPending = false, onChange,
}: {
  agent: string;
  draft: AgentDraft;
  /** What this layer has SAVED for the agent — the values a press of Save is
   *  about to replace. Not the effective ones; see `effective`. */
  stored: AgentLLMOverride | null;
  /** The model this agent will actually call — the override, else inherited. */
  model: string;
  caps: ModelCapabilities | null;
  loading: boolean;
  failed: boolean;
  /** The largest output ceiling this form will accept for this model. */
  limit: number;
  error: "range" | "over" | null;
  disabled: boolean;
  inheritsFrom?: AgentInheritsFrom;
  /** What is in force when this layer sets nothing — the values the empty
   *  boxes inherit. Null while the screen that owns them has not answered. */
  effective?: Pick<
    AgentSettings, "effective_max_output_tokens" | "effective_reasoning"
    | "effective_temperature"
  > | null;
  /** The inherited values are still being fetched. Without this, an empty box
   *  claims "nothing set" for a value that is merely late — a claim, not a
   *  gap, and the operator acts on claims. */
  inheritedPending?: boolean;
  onChange: (patch: Partial<AgentDraft>) => void;
}) {
  const t = useT();
  const known = caps?.known === true;
  const ceiling = known ? caps!.max_output_tokens : null;
  const kind = caps?.reasoning_kind ?? null;
  const values = caps?.reasoning_values ?? null;
  const reasoningAllowed = takesReasoning(caps);
  // Null caps mean the lookup never answered — not that the model cannot
  // reason. The whole reasoning half of this row hangs off that distinction:
  // it decides whether the stored value survives the next Save, and the two
  // states look identical unless the row says which one it is in.
  const capsUnresolved = caps === null;
  // What the operator will actually save from the BOX. A stale reasoning
  // value on a model that cannot take one is dropped by the card, so it must
  // not be shown as an override here either.
  const liveReasoning = reasoningAllowed ? draft.reasoning : "";
  // The stored override the box cannot show, because the control is disabled.
  // It is still in force, and (when the lookup is unresolved) still saved —
  // so it is rendered in the disabled control rather than blanked out. A
  // read-only box showing nothing is how an operator concludes that a setting
  // they configured has vanished.
  const frozenReasoning = reasoningAllowed ? null : storedReasoning(stored);
  // What the provider has refused for this model — measured on a real call,
  // with the sentence and the date. The words stay in the dropdown, struck,
  // with that sentence beside them; the alternative was the option vanishing
  // between two page loads and the operator's saved value running nowhere.
  const reasoningRefusals = providerRefusals(caps, "reasoning");
  const offeredWords = new Set((values ?? []).map((v) => v.trim().toLowerCase()));
  const refusedWords = Array.from(new Set(
    reasoningRefusals
      .map((r) => String(r.value ?? "").trim())
      .filter((w) => w && !offeredWords.has(w.toLowerCase())),
  ));
  // The value this row is about to SAVE, if it is one the provider refuses:
  // the box when the control is live, the frozen stored value when it is not.
  // Either way the runtime drops it on every call and the review runs
  // without it — that has to be said in red, on the row, with the remedy.
  const boxRefusal = refusalFor(
    reasoningAllowed ? liveReasoning : frozenReasoning, caps, "reasoning",
  );
  // The stored value is refused but the box now holds something else: the
  // operator has already picked the fix and only Save is left.
  const storedRefusal = refusalFor(storedReasoning(stored), caps, "reasoning");
  const temperatureFixed = temperatureFixedToDefault(caps);
  const overridden = Boolean(
    draft.model.trim() || draft.maxOut.trim() || (draft.temperature ?? "").trim()
    || liveReasoning.trim()
    || frozenReasoning != null,
  );
  const fromWorkspace = inheritsFrom === "workspace";

  const showInherited = (v: string | number | null | undefined): string =>
    inheritedPending
      ? t("settings.llm.agents.inheritedPending")
      : v == null || v === ""
        ? t("settings.llm.agents.effectiveNone")
        : String(v);

  /** The muted line under the model: what this model can do, or why we cannot
   *  say. Never a default filled in on the model's behalf. */
  const capsLine = (): string => {
    if (loading) return t("settings.llm.agents.capsLoading");
    if (!model) return t("settings.llm.agents.capsNoModel");
    if (failed) return t("settings.llm.agents.capsError");
    if (!caps) return t("settings.llm.agents.capsLoading");
    if (!caps.known) return t("settings.llm.agents.capsUnknown");
    return [
      ceiling != null
        ? t("settings.llm.agents.capsCeiling", { max: ceiling })
        : t("settings.llm.agents.capsCeilingUnknown"),
      caps.supports_reasoning === false
        ? t("settings.llm.agents.capsReasoningNo")
        : !reasoningAllowed
          // litellm says it reasons, but names no parameter we could send —
          // which for this form is the same as not reasoning, and saying so
          // is more useful than repeating the flag.
          ? t("settings.llm.agents.capsReasoningUnsendable")
          : kind === "effort"
            ? t("settings.llm.agents.capsReasoningEffort")
            : t("settings.llm.agents.capsReasoningBudget"),
    ].join(" · ");
  };

  const inheritLabel = t("settings.llm.agents.inheritOption");

  /** Visible in every state, disabled in the ones that cannot take a value:
   *  the operator is choosing BETWEEN models here, and "this one cannot think"
   *  is the sort of thing that decides it. */
  const reasoningControl = () => {
    if (!reasoningAllowed) {
      // The stored value when there is one, not a blank: a disabled box
      // reading "inherit" over an override that is still in force is the
      // screen telling the operator their setting is gone while the server
      // still holds it.
      const shown = frozenReasoning != null ? String(frozenReasoning) : "";
      return (
        <Select className="w-full" disabled value={shown}
          options={[{ value: shown, label: shown || inheritLabel }]} onChange={() => {}} />
      );
    }
    if (kind === "effort") {
      return (
        <Select className="w-full" disabled={disabled} value={liveReasoning}
          onChange={(v) => onChange({ reasoning: v })}
          options={[
            { value: "", label: inheritLabel },
            ...(values ?? []).map((v) => ({ value: v, label: v })),
            // Refused words listed and struck, not omitted: a saved value
            // that is among them still has to be SHOWN as the current
            // value, and a word that is simply gone explains nothing.
            ...refusedWords.map((v) => ({
              value: v, label: v, disabled: true,
              hint: t("settings.llm.agents.refusedOptionHint"),
            })),
          ]} />
      );
    }
    // A budget in tokens. 0 is a real value — it means "do not think".
    return (
      <Input type="number" min={0} max={AGENT_TOKENS_MAX}
        disabled={disabled} value={liveReasoning}
        placeholder={t("settings.llm.agents.inheritPlaceholder")}
        onChange={(e) => onChange({ reasoning: e.target.value })} />
    );
  };

  /** Why the control above is disabled. FIVE different silences, and they used
   *  to collapse into two strings — so "we could not reach the capabilities
   *  endpoint" and "this model cannot think" read the same while meaning
   *  opposite things: one leaves the stored setting untouched, the other is
   *  about to remove it. The operator's next move differs for every branch:
   *  wait, reload, pick a model, swap the model, or accept it. */
  const reasoningDisabledReason = (): string => {
    if (loading) return t("settings.llm.agents.reasoningChecking");
    if (failed) return t("settings.llm.agents.reasoningLookupFailed");
    if (!model) return t("settings.llm.agents.reasoningNoModel");
    if (!known) return t("settings.llm.agents.reasoningUnknown");
    if (caps!.supports_reasoning === false) return t("settings.llm.agents.reasoningNone");
    // A sixth silence, added with the refusals: the model reasons and the
    // router would translate a word, but the provider has refused every word
    // this build offers, so the list is empty for a reason "LiteLLM names no
    // parameter" would misstate. The lines under the control say which
    // words and when.
    if (kind === "effort" && reasoningRefusals.length > 0 && !(values?.length)) {
      return t("settings.llm.agents.reasoningAllRefused");
    }
    return t("settings.llm.agents.reasoningUnsendable");
  };

  /** One refusal, as a sentence the operator can act on: which model refused
   *  which word, when, and in whose words. The date is the part the old
   *  dropdown could never give — "refused on <date>" is a fact to check
   *  against a provider's changelog; a missing option is a mystery. */
  const refusalSentence = (r: ProviderRefusal): string => {
    const reason = r.reason?.trim() || t("settings.llm.agents.refusalNoReason");
    const value = String(r.value ?? "—");
    return r.seen_at
      ? t("settings.llm.agents.refusalLine",
          { model: model || caps?.model || "", value, date: formatDate(r.seen_at), reason })
      : t("settings.llm.agents.refusalLineNoDate",
          { model: model || caps?.model || "", value, reason });
  };

  const reasoningHint = (): string => {
    if (!reasoningAllowed) {
      const why = reasoningDisabledReason();
      if (frozenReasoning == null) return why;
      // There IS a stored value under that disabled control, and the two
      // states do opposite things to it on the next Save. Say which.
      return `${why} ${t(
        capsUnresolved
          ? "settings.llm.agents.reasoningKept"
          : "settings.llm.agents.reasoningDropped",
        { value: String(frozenReasoning) },
      )}`;
    }
    if (liveReasoning.trim()) {
      return kind === "budget"
        ? t("settings.llm.agents.reasoningBudgetHint")
        : t("settings.llm.agents.reasoningEffortHint");
    }
    return t(
      fromWorkspace
        ? "settings.llm.agents.reasoningInheritsWorkspace"
        : "settings.llm.agents.reasoningInherits",
      { value: showInherited(effective?.effective_reasoning) },
    );
  };

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/20 px-3 py-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium capitalize">{agent}</span>
        <Badge variant={overridden ? "brand" : "outline"} className="text-[10px]">
          {overridden
            ? t("settings.llm.agents.overriddenBadge")
            : t("settings.llm.agents.inheritedBadge")}
        </Badge>
      </div>
      <div>
        <Label>{t("settings.llm.agents.modelLabel")}</Label>
        {/* One combobox and one catalog answer the question on both screens —
            and the empty string means "inherit" in both. */}
        <ModelSelect
          value={draft.model}
          placeholder={inheritLabel}
          disabled={disabled}
          // A model whose provider has no key here degrades every review
          // silently, at runtime. Typing one by hand still works — that is how
          // a self-hosted name gets in.
          onlyAvailable
          onChange={(v) => onChange({ model: v })}
        />
        <p className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">
          {!draft.model.trim() && model
            ? t(
                fromWorkspace
                  ? "settings.llm.agents.inheritsModelWorkspace"
                  : "settings.llm.agents.inheritsModel",
                { model },
              ) + " · "
            : ""}
          {capsLine()}
        </p>
        {caps?.supports_function_calling === false && (
          <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
            {t("settings.llm.agents.noToolsWarning")}
          </p>
        )}
        {/* Temperature has no control anywhere — Celmis sends one value to
            every review agent — so a model that takes only its own default
            (claude-sonnet-5 answers 400 to anything but 1) used to be a fact
            the operator met only as a dropped call on a run. Learned from
            the provider, said on the model it is about, with the date. */}
        {temperatureFixed && (
          <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
            {temperatureFixed.seen_at
              ? t("settings.llm.agents.temperatureFixed", {
                  value: String(temperatureFixed.value ?? "—"),
                  date: formatDate(temperatureFixed.seen_at),
                  reason: temperatureFixed.reason?.trim()
                    || t("settings.llm.agents.refusalNoReason"),
                })
              : t("settings.llm.agents.temperatureFixedNoDate", {
                  reason: temperatureFixed.reason?.trim()
                    || t("settings.llm.agents.refusalNoReason"),
                })}
          </p>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label>{t("settings.llm.agents.maxOutLabel")}</Label>
          <Input
            type="number" min={AGENT_TOKENS_MIN}
            // The narrower of the model's own ceiling and the server's bound,
            // so the browser's stepper and validation refuse a value before
            // the request is ever built.
            max={limit}
            disabled={disabled}
            aria-invalid={error != null}
            value={draft.maxOut}
            placeholder={t("settings.llm.agents.inheritPlaceholder")}
            onChange={(e) => onChange({ maxOut: e.target.value })}
          />
          <p className={cn(
            "mt-1 text-[11px]",
            error ? "text-red-600 dark:text-red-400" : "text-[var(--color-muted-foreground)]",
          )}>
            {error === "over"
              ? t("settings.llm.agents.maxOutTooBig", { max: ceiling ?? limit })
              : error === "range"
                ? t("settings.llm.agents.maxOutRange", {
                    min: AGENT_TOKENS_MIN, max: AGENT_TOKENS_MAX })
                : draft.maxOut.trim()
                  ? (ceiling != null
                      ? t("settings.llm.agents.maxOutHelp", { max: ceiling })
                      : t("settings.llm.agents.maxOutUnknown"))
                  : t(
                      fromWorkspace
                        ? "settings.llm.agents.maxOutInheritsWorkspace"
                        : "settings.llm.agents.maxOutInherits",
                      { value: showInherited(effective?.effective_max_output_tokens) },
                    )}
          </p>
        </div>
        <div>
          <Label>{t("settings.llm.agents.temperatureLabel")}</Label>
          <Input
            type="number" min={0} max={2} step={0.1}
            disabled={disabled}
            value={draft.temperature ?? ""}
            placeholder={t("settings.llm.agents.inheritPlaceholder")}
            onChange={(e) => onChange({ temperature: e.target.value })}
          />
          <p className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">
            {(draft.temperature ?? "").trim()
              ? t("settings.llm.agents.temperatureHelp")
              : t("settings.llm.agents.temperatureInherits", {
                  value: showInherited(effective?.effective_temperature),
                })}
          </p>
        </div>
        <div>
          <Label>{t("settings.llm.agents.reasoningLabel")}</Label>
          {reasoningControl()}
          <p className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">
            {reasoningHint()}
          </p>
          {/* Every refusal on file for this model, with the provider's own
              sentence and the date it was learned — the explanation the
              struck options above are pointing at. */}
          {reasoningRefusals.map((r, i) => (
            <p key={`${String(r.value)}-${i}`}
              className="mt-1 text-[11px] text-amber-600 dark:text-amber-400 wrap-anywhere">
              {refusalSentence(r)}
            </p>
          ))}
          {/* THE CONFLICT, LOUD. The value this row is about to save is one
              the provider refuses: the runtime drops it on every call and the
              review runs without it, from the second run onward, saying
              nothing. It is NOT deleted on save — an earlier wave made
              untouched values survive the press, and a setting that vanishes
              is the bug this surface exists to end — so the row says it in
              red, with the two fixes: another level here, or a fallback model
              that will answer in its place. */}
          {boxRefusal && (
            <p className="mt-1 text-[11px] text-red-600 dark:text-red-400 wrap-anywhere">
              {t("settings.llm.agents.savedReasoningRefused", {
                value: String(boxRefusal.value ?? ""),
              })}{" "}
              <Link className="underline" href="/settings/llm#review-fallback">
                {t("settings.llm.agents.savedReasoningRefusedLink")}
              </Link>.
            </p>
          )}
          {/* The operator has already picked a level the provider takes; the
              saved one is still the refused word until Save is pressed. */}
          {!boxRefusal && storedRefusal && reasoningAllowed && (
            <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400 wrap-anywhere">
              {t("settings.llm.agents.savedReasoningRefusedReplaced", {
                value: String(storedRefusal.value ?? ""),
              })}
            </p>
          )}
        </div>
      </div>
      {/* The dead end this row was moved here to end: the repo policy WINS
          over the workspace entry, so the operator with the most authority is
          the one who most needs to know the other screen exists. */}
      {fromWorkspace && (
        <p className="text-[11px] text-[var(--color-muted-foreground)]">
          {t("settings.llm.agents.workspaceDefaultsNote")}{" "}
          <Link className="underline" href="/settings/llm#review-agents">
            {t("settings.llm.agents.workspaceDefaultsLink")}
          </Link>.
        </p>
      )}
    </div>
  );
}
