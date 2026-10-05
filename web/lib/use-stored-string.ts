"use client";

/**
 * One string in localStorage, read as a store rather than copied into state.
 *
 * A mount effect that reads localStorage and calls setState renders the
 * default first and the stored value a frame later — the same flash the
 * sidebar had (see lib/sidebar.ts). `useSyncExternalStore` hydrates with the
 * server snapshot (null, so the default) and switches to the stored value in
 * the same commit, without an effect that sets state.
 *
 * Every access is wrapped: Safari's private mode, a blocked site-data policy
 * and a full quota all throw from `localStorage`, and a preference is never
 * worth a broken page. When writing fails the value is kept in memory for the
 * life of the tab, so the control still responds — it just is not
 * remembered next time.
 */

import { useCallback, useSyncExternalStore } from "react";

const memory = new Map<string, string>();
const listeners = new Set<() => void>();

export function readStoredString(key: string): string | null {
  if (memory.has(key)) return memory.get(key) ?? null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStoredString(key: string, value: string): void {
  memory.set(key, value);
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private mode, blocked storage or quota — the memory copy serves this tab */
  }
  listeners.forEach((l) => l());
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  // Another tab wrote: its value wins over this tab's memory copy.
  const onStorage = (e: StorageEvent) => {
    if (e.key === null) memory.clear();
    else memory.delete(e.key);
    onChange();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onStorage);
  };
}

/** `[value, set]` — `value` is null on the server, on the first hydrating
 *  render, and whenever nothing is stored (or storage is unreadable). */
export function useStoredString(key: string): [string | null, (value: string) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => readStoredString(key),
    () => null,
  );
  const set = useCallback((next: string) => writeStoredString(key, next), [key]);
  return [value, set];
}
