/**
 * The logic behind the token form's pickers, kept free of React so it can be tested
 * in plain node: filtering a list as you type, moving the highlighted row with the
 * keyboard, and the tri-state "select all" over a (possibly filtered) checklist.
 */

export type PickerOption = {
  value: string;
  label: string;
  /** Secondary text, shown muted next to the label and searched too. */
  hint?: string;
  /** More text to search on (a slug, a second name) that is not shown. */
  keywords?: string[];
};

const fold = (s: string) =>
  s.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();

/** Options matching every word of `query` (case- and accent-insensitive) in the
 *  label, hint or keywords. An empty query keeps the whole list, in order. */
export function filterOptions<T extends PickerOption>(options: readonly T[], query: string): T[] {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...options];
  return options.filter((o) => {
    const hay = fold([o.label, o.hint ?? "", ...(o.keywords ?? [])].join(" "));
    return words.every((w) => hay.includes(w));
  });
}

export type ComboKey = "ArrowDown" | "ArrowUp" | "Home" | "End" | "Enter" | "Escape" | "Tab";

export type ComboStep = {
  open: boolean;
  active: number;
  /** Pick the row at `active`. */
  pick: boolean;
  /** The key was used: the caller calls preventDefault. */
  handled: boolean;
};

/** What a key does to a combobox with `count` rows. ArrowDown on a closed list
 *  opens it; arrows wrap; Enter picks only while open and a row is highlighted;
 *  Escape closes (and is handled only while open, so it still closes a dialog
 *  around the field when the list is shut). */
export function comboStep(
  state: { open: boolean; active: number }, key: string, count: number,
): ComboStep {
  const idle = { ...state, pick: false, handled: false };
  switch (key) {
    case "ArrowDown":
    case "ArrowUp": {
      if (!state.open) return { open: true, active: state.active, pick: false, handled: true };
      if (count === 0) return { ...idle, handled: true };
      const step = key === "ArrowDown" ? 1 : -1;
      return { open: true, active: (state.active + step + count) % count, pick: false, handled: true };
    }
    case "Home":
      return state.open && count > 0 ? { open: true, active: 0, pick: false, handled: true } : idle;
    case "End":
      return state.open && count > 0
        ? { open: true, active: count - 1, pick: false, handled: true } : idle;
    case "Enter":
      return state.open && count > 0 && state.active >= 0 && state.active < count
        ? { open: false, active: state.active, pick: true, handled: true } : idle;
    case "Escape":
      return state.open ? { open: false, active: state.active, pick: false, handled: true } : idle;
    case "Tab":
      return { ...idle, open: false };
    default:
      return idle;
  }
}

// ─── select all ──────────────────────────────────────────────────────

export type TriState = "none" | "some" | "all";

export type SelectAll = {
  state: TriState;
  /** Ticked rows among the ones that can be ticked or already are. */
  ticked: number;
  /** Rows the control acts on: visible and either ticked or tickable. */
  total: number;
};

/** The state of the "select all" box over `visible` ids. `canAdd(id, picked)` says
 *  whether an unticked row may be ticked now (a full share, the per-token cap); an
 *  untickable row is left out of the count, so the box can still read "all". */
export function selectAllState(
  visible: readonly string[], picked: readonly string[],
  canAdd: (id: string, picked: readonly string[]) => boolean = () => true,
): SelectAll {
  const set = new Set(picked);
  const rows = visible.filter((id) => set.has(id) || canAdd(id, picked));
  const ticked = rows.filter((id) => set.has(id)).length;
  const state: TriState = ticked === 0 ? "none" : ticked === rows.length ? "all" : "some";
  return { state, ticked, total: rows.length };
}

/** The new selection after clicking "select all": everything visible goes on unless
 *  it is all on already, then everything visible goes off. Rows hidden by the filter
 *  keep their state. `canAdd` is asked again as rows are added, so a cap holds. */
export function toggleAllVisible(
  visible: readonly string[], picked: readonly string[],
  canAdd: (id: string, picked: readonly string[]) => boolean = () => true,
): string[] {
  const { state } = selectAllState(visible, picked, canAdd);
  if (state === "all") {
    const off = new Set(visible);
    return picked.filter((id) => !off.has(id));
  }
  const next = [...picked];
  for (const id of visible) {
    if (!next.includes(id) && canAdd(id, next)) next.push(id);
  }
  return next;
}
