"use client";

/**
 * Review categories: which agents review, and on which model with which
 * limits. One card per kind of finding (Bug, Contract, Security, …), named
 * the way findings are filed (lib/review-categories.ts).
 *
 * Participation is two lists on the server — `disabled_agents` for agents
 * that run by default, `enabled_agents` for the opt-in ones — and one switch
 * per card here; `switchAgent` in the model writes the right list. A card is
 * "Overridden" when it differs from what this scope inherits, and its reset
 * puts that one agent back, not the whole list.
 */

import Link from "next/link";
import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { ChevronDownIcon, CpuIcon, RotateCcwIcon } from "lucide-react";

import {
  api, type ConnectionStatus, type JiraFieldOption, type JiraProjectOption,
} from "@/lib/api";
import { AGENT_CATEGORY, agentLabel } from "@/lib/review-categories";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";
import {
  AgentLLMRow, DEFAULT_AGENT_MAX_OUTPUT, agentDraftFrom, agentMaxOutLimit,
} from "@/components/agent-llm-controls";
import { Badge } from "@/components/ui/badge";
import { OverriddenPill } from "@/components/ui/status";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, ChoiceRow, Group, OriginBadge, ResetButton, SectionFrame, SettingRow,
} from "@/components/review-settings/field";
import {
  effective, effectiveParticipation, inheritedParticipation, isSet, switchAgent,
} from "@/components/review-settings/model";
import { settingsHref } from "@/lib/review-settings-routes";

/** The cards, in the order a reader thinks about them: the finders first,
 *  then the checks that are not switchable, then the verifier stage. */
const FINDER_ORDER = [
  "defect", "contract", "security", "performance", "business_logic", "structural", "cve",
];
/** Run on every review; not on/off per scope. */
const ALWAYS_ON = ["compliance", "breaking_change"] as const;

const ROLE_KEY: Record<string, string> = {
  defect: "admin.reviewPolicies.agentRole.defect",
  contract: "admin.reviewPolicies.agentRole.contract",
  security: "admin.reviewPolicies.agentRole.security",
  performance: "admin.reviewPolicies.agentRole.performance",
  business_logic: "admin.reviewPolicies.agentRole.business_logic",
  structural: "admin.reviewPolicies.agentRole.structural",
  verifier: "admin.reviewPolicies.agentRole.verifier",
  cve: "reviewSettings.agents.role.cve",
  compliance: "reviewSettings.agents.role.compliance",
  breaking_change: "reviewSettings.agents.role.breaking_change",
};

export function CategoriesSection() {
  const t = useT();
  const { scope, draft, inh, meta, patch, setOwn, llm, canEdit } = useSettings();
  const defaults = meta.participationDefaults;
  const finders = [
    ...FINDER_ORDER.filter((a) => a in defaults),
    ...Object.keys(defaults).filter((a) => !FINDER_ORDER.includes(a)),
  ];
  const now = effectiveParticipation(draft, inh, defaults);
  const before = inheritedParticipation(inh, defaults);
  const flip = (agent: string, on: boolean) => {
    const next = switchAgent(draft, inh, defaults, agent, on);
    patch({ own: { ...draft.own, ...next } });
  };
  const verifierOn = Boolean(effective(draft, "verifier_enabled", scope.kind, inh));
  const listsSet = isSet(draft, "disabled_agents", scope.kind) || isSet(draft, "enabled_agents", scope.kind);

  return (
    <SectionFrame
      id="categories"
      title={t("reviewSettings.section.categories")}
      description={t("reviewSettings.categories.desc")}
    >
      <Group
        title={t("reviewSettings.categories.findersGroup")}
        description={t("admin.reviewPolicies.detail.agentToggleCostNote")}
        action={listsSet && canEdit ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => patch({ own: { ...draft.own, disabled_agents: null, enabled_agents: null } })}
          >
            <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
            {scope.kind === "repo"
              ? t("reviewSettings.categories.resetAllRepo")
              : t("reviewSettings.categories.resetAllGlobal")}
          </Button>
        ) : undefined}
      >
        {finders.map((agent) => {
          const on = now[agent];
          const differs = on !== before[agent];
          return (
            <AgentCard
              key={agent}
              agent={agent}
              optIn={defaults[agent] === false}
              on={on}
              overridden={differs}
              onToggle={(v) => flip(agent, v)}
              onReset={() => flip(agent, before[agent])}
              withModel={llm.names.includes(agent)}
            />
          );
        })}
      </Group>

      <JiraTaskGroup />

      <Group
        title={t("reviewSettings.categories.alwaysGroup")}
        description={t("reviewSettings.categories.alwaysGroupHint")}
      >
        {ALWAYS_ON.map((agent) => (
          <AgentCard
            key={agent}
            agent={agent}
            fixed
            on
            withModel={llm.names.includes(agent)}
            extra={agent === "compliance" ? (
              <Link href="/admin/compliance" className="text-xs font-medium text-[var(--color-primary)] underline-offset-4 hover:underline">
                {t("reviewSettings.categories.complianceLink")}
              </Link>
            ) : undefined}
          />
        ))}
      </Group>

      <Group
        title={t("reviewSettings.categories.verifierGroup")}
        description={t("admin.reviewPolicies.detail.verifierOptIn")}
      >
        <AgentCard
          agent="verifier"
          on={verifierOn}
          field
          onToggle={(v) => setOwn("verifier_enabled", v)}
          onReset={() => setOwn("verifier_enabled", null)}
          withModel={llm.names.includes("verifier")}
        />
      </Group>

      {scope.kind === "repo" && !llm.names.includes("compliance") && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("reviewSettings.categories.complianceModelAtGlobal")}{" "}
          <Link className="underline underline-offset-4" href={settingsHref({ section: "categories" })}>
            {t("reviewSettings.scope.global")}
          </Link>
        </p>
      )}
    </SectionFrame>
  );
}

function AgentCard({
  agent, on, optIn, overridden, onToggle, onReset, fixed, field, withModel, extra,
}: {
  agent: string;
  on: boolean;
  optIn?: boolean;
  /** Differs from what this scope inherits. */
  overridden?: boolean;
  onToggle?: (v: boolean) => void;
  onReset?: () => void;
  /** Not switchable here. */
  fixed?: boolean;
  /** The verifier: its switch is one inheritable field. */
  field?: boolean;
  withModel: boolean;
  extra?: React.ReactNode;
}) {
  const t = useT();
  const { scope, canEdit, focusAgent } = useSettings();
  const id = useId();
  const category = AGENT_CATEGORY[agent];
  const title = agent === "verifier" ? t("reviewSettings.agents.verifier") : category ?? agentLabel(agent);
  const [open, setOpen] = useState(focusAgent === agent);
  return (
    <div className="px-4 py-3.5" id={`agent-${agent}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex min-h-8 flex-wrap items-center gap-x-2 gap-y-1">
            <label htmlFor={fixed ? undefined : id} className="text-sm font-medium">{title}</label>
            {category && category !== agentLabel(agent) && (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {t("reviewSettings.agents.agentName", { name: agentLabel(agent) })}
              </span>
            )}
            {optIn && (
              <Badge variant="outline" className="text-[10px] font-normal">{t("reviewSettings.agents.optIn")}</Badge>
            )}
            {fixed && (
              <Badge variant="outline" className="text-[10px] font-normal">{t("reviewSettings.agents.always")}</Badge>
            )}
            {field && <OriginBadge field="verifier_enabled" />}
            {!field && !fixed && overridden && (
              <OverriddenPill
                label={scope.kind === "repo"
                  ? t("reviewSettings.origin.overridden")
                  : t("reviewSettings.origin.workspaceSet")}
              />
            )}
            {(field ? undefined : overridden) && onReset && canEdit && <ResetButton onClick={onReset} />}
            {field && <VerifierReset onReset={onReset} />}
          </div>
          <p className="mt-0.5 max-w-[70ch] text-xs text-[var(--color-muted-foreground)]">
            {t(ROLE_KEY[agent] ?? "reviewSettings.agents.role.generic")}
          </p>
          {extra && <div className="mt-1">{extra}</div>}
        </div>
        {!fixed && (
          <div className="flex shrink-0 items-center gap-2 pt-1.5">
            <span className={cn("text-xs tabular-nums", on ? "text-[var(--color-foreground)]" : "text-[var(--color-muted-foreground)]")}>
              {on ? t("reviewSettings.value.on") : t("reviewSettings.value.off")}
            </span>
            <Switch
              id={id}
              checked={on}
              disabled={!canEdit}
              onCheckedChange={(v) => onToggle?.(v)}
            />
          </div>
        )}
      </div>
      {withModel && (
        <div className="mt-2">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={`${id}-llm`}
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
          >
            <CpuIcon className="h-3.5 w-3.5" aria-hidden />
            <AgentModelSummary agent={agent} />
            <ChevronDownIcon
              aria-hidden
              className={cn("h-3.5 w-3.5 transition-transform duration-200", open && "rotate-180")}
            />
          </button>
          <AnimatePresence initial={false}>
            {open && (
              <m.div
                id={`${id}-llm`}
                key="llm"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: [0.23, 1, 0.32, 1] }}
                className="overflow-hidden"
              >
                <div className="pt-2">
                  <AgentModelRow agent={agent} />
                </div>
              </m.div>
            )}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}

function VerifierReset({ onReset }: { onReset?: () => void }) {
  const { draft, canEdit } = useSettings();
  const set = draft.own.verifier_enabled !== null && draft.own.verifier_enabled !== undefined;
  if (!set || !onReset || !canEdit) return null;
  return <ResetButton onClick={onReset} />;
}

/** "gemini-2.5-pro · 16,384 tokens", or "Model & limits" — and whether this
 *  scope sets any of them. */
function AgentModelSummary({ agent }: { agent: string }) {
  const t = useT();
  const { draft, original, llm } = useSettings();
  const d = draft.agentLLM[agent] ?? agentDraftFrom(null);
  const own = Boolean(d.model.trim() || d.maxOut.trim() || d.reasoning.trim() || (d.temperature ?? "").trim());
  const model = llm.models[agent];
  const changed = JSON.stringify(draft.agentLLM[agent]) !== JSON.stringify(original.agentLLM[agent]);
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <span>{t("reviewSettings.agents.modelAndLimits")}</span>
      {model && <span className="font-mono text-[11px]">{model}</span>}
      {own && (
        <OverriddenPill label={t("reviewSettings.agents.modelSet")} className="px-1.5 text-[10px]" />
      )}
      {changed && <span className="sr-only">{t("reviewSettings.save.unsavedShort")}</span>}
    </span>
  );
}

function AgentModelRow({ agent }: { agent: string }) {
  const t = useT();
  const { scope, draft, patch, canEdit, llm } = useSettings();
  const caps = llm.caps[agent] ?? { caps: null, loading: false, failed: false };
  return (
    <div className="space-y-2">
      <AgentLLMRow
        agent={agent}
        inheritsFrom={scope.kind === "repo" ? "workspace" : "profile"}
        draft={draft.agentLLM[agent] ?? agentDraftFrom(null)}
        stored={(llm.stored[agent] as never) ?? null}
        effective={llm.inherited?.[agent] ?? null}
        inheritedPending={!llm.ready}
        model={llm.models[agent] ?? ""}
        caps={caps.caps}
        loading={caps.loading}
        failed={caps.failed}
        limit={agentMaxOutLimit(caps.caps)}
        error={llm.maxOutErrors[agent] ?? null}
        disabled={!canEdit || !llm.ready}
        onChange={(p) => patch({
          agentLLM: {
            ...draft.agentLLM,
            [agent]: { ...(draft.agentLLM[agent] ?? agentDraftFrom(null)), ...p },
          },
        })}
      />
      <Callout tone="info">
        {t("settings.llm.agents.budgetNote", { tokens: DEFAULT_AGENT_MAX_OUTPUT })}
      </Callout>
    </div>
  );
}

// ─── the Jira task the business-logic agent reads ────────────────────

/** Project keys as a person types them: split on commas, spaces, semicolons. */
function parseProjectKeys(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((k) => k.trim().toUpperCase()).filter(Boolean))];
}
const PROJECT_KEY_RE = /^[A-Z][A-Z0-9_]{1,9}$/;
const FIELD_ID_RE = /^customfield_\d{1,9}$/;
const INCLUDE_COMMENTS_MAX = 10;

function JiraTaskGroup() {
  const t = useT();
  const token = useToken();
  const { scope, draft, inh, meta } = useSettings();
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: () => api<ConnectionStatus[]>("/api/connections", { token }),
    enabled: !!token,
  });
  const connected = Boolean(conns.data?.find((c) => c.provider === "jira")?.connected);
  const on = Boolean(effective(draft, "task_context_enabled", scope.kind, inh));
  const served = meta.choices.business_logic_auto ?? ["off", "when_task_found"];
  const options = ["off", "when_task_found"].filter((v) => served.includes(v)).map((v) => ({
    value: v,
    title: t(`reviewSettings.jira.auto.${v}`),
    body: t(`reviewSettings.jira.auto.${v}Body`),
  }));
  const modes = ["off", "findings", "checklist"];
  const servedModes = meta.choices.requirements_check_mode ?? modes;
  const modeOptions = modes.filter((v) => servedModes.includes(v)).map((v) => ({
    value: v,
    title: t(`reviewSettings.jira.requirements.${v}`),
    body: t(`reviewSettings.jira.requirements.${v}Body`),
  }));
  return (
    <Group
      title={t("reviewSettings.jira.group")}
      description={t("reviewSettings.jira.groupHint")}
    >
      {conns.isSuccess && !connected && (
        <div className="px-4 pt-3.5">
          <Callout tone="info">
            {t("reviewSettings.jira.notConnected")}{" "}
            <Link href="/connections" className="font-medium underline underline-offset-4">
              {t("reviewSettings.jira.connect")}
            </Link>
          </Callout>
        </div>
      )}
      <BooleanRow
        field="task_context_enabled"
        label={t("reviewSettings.jira.enabled")}
        description={t("reviewSettings.jira.enabledHint")}
      />
      <ChoiceRow
        field="business_logic_auto"
        label={t("reviewSettings.jira.auto")}
        description={t("reviewSettings.jira.autoHint")}
        options={options}
        columns={2}
        disabled={!on}
      />
      <ChoiceRow
        field="requirements_check_mode"
        label={t("reviewSettings.jira.requirements")}
        description={t("reviewSettings.jira.requirementsHint")}
        options={modeOptions}
        columns={3}
        disabled={!on}
      />
      <BooleanRow
        field="task_urls_enabled"
        label={t("reviewSettings.jira.urls")}
        description={t("reviewSettings.jira.urlsHint")}
        disabled={!on}
      />
      <ProjectKeysRow disabled={!on} connected={connected} />
      <AcceptanceFieldRow disabled={!on} connected={connected} />
      <IncludeCommentsRow disabled={!on} />
    </Group>
  );
}

function ProjectKeysRow({ disabled, connected }: { disabled: boolean; connected: boolean }) {
  const t = useT();
  const token = useToken();
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own.task_project_keys as string[] | null;
  const inherited = Array.isArray(inh.values.task_project_keys)
    ? (inh.values.task_project_keys as string[]) : [];
  const [text, setText] = useState(own ? own.join(", ") : "");
  // A reset (or a reload) changes the draft behind the box's back.
  const shown = parseProjectKeys(text);
  if (own === null && text !== "") setText("");
  else if (own !== null && shown.join(",") !== own.join(",")) setText(own.join(", "));
  const projects = useQuery({
    queryKey: ["task-context", "projects"],
    queryFn: () => api<JiraProjectOption[]>("/api/task-context/projects", { token }),
    enabled: !!token && connected,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const bad = shown.filter((k) => !PROJECT_KEY_RE.test(k));
  return (
    <SettingRow
      field="task_project_keys"
      label={t("reviewSettings.jira.projects")}
      htmlFor={id}
      description={t("reviewSettings.jira.projectsHint")}
      describeInherited={(v) => {
        const list = Array.isArray(v) ? (v as string[]) : [];
        return list.length ? list.join(", ") : t("reviewSettings.jira.anyProject");
      }}
      control={(
        <div className="space-y-1">
          <Input
            id={id}
            value={text}
            list={`${id}-projects`}
            spellCheck={false}
            autoCapitalize="characters"
            className="max-w-md font-mono text-xs uppercase"
            disabled={!canEdit || disabled}
            placeholder={own === null && inherited.length ? inherited.join(", ") : "PROJ, AIR"}
            aria-invalid={bad.length ? true : undefined}
            onChange={(e) => {
              setText(e.target.value);
              setOwn("task_project_keys", e.target.value.trim() ? parseProjectKeys(e.target.value) : []);
            }}
          />
          {projects.data && (
            <datalist id={`${id}-projects`}>
              {projects.data.map((p) => (
                <option key={p.key} value={p.key}>{p.name}</option>
              ))}
            </datalist>
          )}
          {bad.length > 0 && (
            <p role="alert" className="text-xs text-[var(--color-destructive)]">
              {t("reviewSettings.jira.projectsInvalid", { keys: bad.join(", ") })}
            </p>
          )}
        </div>
      )}
    />
  );
}

function AcceptanceFieldRow({ disabled, connected }: { disabled: boolean; connected: boolean }) {
  const t = useT();
  const token = useToken();
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own.task_acceptance_field as string | null;
  const inherited = String(inh.values.task_acceptance_field ?? "");
  const [pick, setPick] = useState(false);
  const fields = useQuery({
    queryKey: ["task-context", "fields"],
    queryFn: () => api<JiraFieldOption[]>("/api/task-context/fields", { token }),
    enabled: !!token && connected && pick,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const invalid = Boolean(own && !FIELD_ID_RE.test(own));
  return (
    <SettingRow
      field="task_acceptance_field"
      label={t("reviewSettings.jira.field")}
      htmlFor={id}
      description={t("reviewSettings.jira.fieldHint")}
      describeInherited={(v) => (v ? String(v) : t("reviewSettings.jira.fieldNone"))}
      control={(
        <div className="space-y-2">
          <Input
            id={id}
            value={own ?? ""}
            spellCheck={false}
            className="max-w-xs font-mono text-xs"
            disabled={!canEdit || disabled}
            placeholder={inherited || "customfield_10042"}
            aria-invalid={invalid ? true : undefined}
            onChange={(e) => setOwn("task_acceptance_field", e.target.value.trim() || null)}
          />
          {invalid && (
            <p role="alert" className="text-xs text-[var(--color-destructive)]">
              {t("reviewSettings.jira.fieldInvalid")}
            </p>
          )}
          {connected && canEdit && !disabled && (
            pick ? (
              <Select
                value={own ?? ""}
                onChange={(v) => setOwn("task_acceptance_field", v || null)}
                className="max-w-md"
                placeholder={fields.isError
                  ? t("reviewSettings.jira.fieldsFailed")
                  : t("reviewSettings.jira.fieldsLoading")}
                options={(fields.data ?? []).map((f) => ({
                  value: f.id, label: f.name, hint: f.id,
                }))}
              />
            ) : (
              <Button type="button" variant="ghost" size="sm" onClick={() => setPick(true)}>
                {t("reviewSettings.jira.fieldPick")}
              </Button>
            )
          )}
        </div>
      )}
    />
  );
}

function IncludeCommentsRow({ disabled }: { disabled: boolean }) {
  const t = useT();
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own.task_include_comments as number | null;
  return (
    <SettingRow
      field="task_include_comments"
      label={t("reviewSettings.jira.comments")}
      htmlFor={id}
      description={t("reviewSettings.jira.commentsHint")}
      describeInherited={(v) => String(v ?? 0)}
      control={(
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={0}
          max={INCLUDE_COMMENTS_MAX}
          className="w-32 tabular-nums"
          placeholder={String(inh.values.task_include_comments ?? 0)}
          value={own ?? ""}
          disabled={!canEdit || disabled}
          onChange={(e) => {
            const raw = e.target.value.trim();
            if (!raw) return setOwn("task_include_comments", null);
            const n = Math.round(Number(raw));
            if (!Number.isFinite(n)) return;
            setOwn("task_include_comments", Math.min(INCLUDE_COMMENTS_MAX, Math.max(0, n)));
          }}
        />
      )}
    />
  );
}
