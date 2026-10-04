"use client";

/**
 * What every section of the settings page reads: the scope, the draft, what
 * the draft inherits, who may change it, and the server's vocabularies.
 * One context instead of props through eight sections and their rows.
 */

import { createContext, useContext } from "react";

import type {
  AgentInfo,
  AgentSettings,
  ReviewPolicy,
  WorkspaceReviewDefaults,
} from "@/lib/api";
import type { AgentCaps } from "@/components/agent-llm-controls";
import type { Draft, InheritableKey, Inheritance, Scope } from "@/components/review-settings/model";
import type { SectionId } from "@/lib/review-settings-routes";

export type SettingsMeta = {
  /** field → the closed choices the server accepts, built-in first. */
  choices: Record<string, string[]>;
  placeholders: string[];
  languages: string[];
  /** agent → runs by default (false = opt-in through enabled_agents). */
  participationDefaults: Record<string, boolean>;
  /** Agents a repository may give its own system prompt. */
  overridableAgents: string[];
};

export type AgentLLMState = {
  /** Agents with a model row at this scope, in order. */
  names: string[];
  models: Record<string, string>;
  caps: Record<string, AgentCaps>;
  maxOutErrors: Record<string, "range" | "over" | null>;
  /** The inherited side of each row (the workspace LLM config). */
  inherited: Record<string, AgentSettings> | undefined;
  stored: Record<string, Record<string, unknown> | null | undefined>;
  ready: boolean;
};

export type SettingsContextValue = {
  scope: Scope;
  draft: Draft;
  original: Draft;
  inh: Inheritance;
  meta: SettingsMeta;
  setOwn: (key: InheritableKey, value: unknown) => void;
  patch: (p: Partial<Draft>) => void;
  /** May change this scope's settings (not prompts — see below). */
  canEdit: boolean;
  /** May change agent prompts at this scope. At Global the editor role may,
   *  while the other defaults are the owner's and admin's. */
  canEditPrompts: boolean;
  defaults?: WorkspaceReviewDefaults;
  policy?: ReviewPolicy;
  wsAgents?: AgentInfo[];
  llm: AgentLLMState;
  /** An agent a link asked to open (`?agent=`). */
  focusAgent: string | null;
  /** Go to another section of the same scope. */
  goTo: (section: SectionId) => void;
};

export const SettingsContext = createContext<SettingsContextValue | null>(null);

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings outside the review settings page");
  return ctx;
}
