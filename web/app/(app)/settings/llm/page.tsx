"use client";

/**
 * /settings/llm — provider + model configuration for every LLM surface.
 *
 *   Provider keys  (shared, one per vendor)
 *   Chat / Q&A     → provider + model   (streamed answers)
 *   Celmis agent   → provider + model   (reads a sentence into a plan)
 *   PR Review      → provider + model   (review agents)
 *   Embeddings     → provider + model   (vector search; switching → re-index)
 *
 * Each surface picks its own provider+model independently; keys are shared.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import {
  BrainIcon, DatabaseIcon, EyeIcon, EyeOffIcon, GitPullRequestIcon, KeyIcon,
  MessagesSquareIcon, RefreshCwIcon, SaveIcon, ServerIcon, WandIcon, ZapIcon,
} from "lucide-react";

import {
  llmApi, vectorStoreApi, workspacesApi, REVIEW_AGENTS,
  type AgentLLMOverride, type AgentSettings, type LLMConfig, type LLMProfile,
  type LLMSurface, type ReviewAgent, type TestConnectionResult,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import {
  AgentLLMRow, DEFAULT_AGENT_MAX_OUTPUT, agentDraftFrom, agentEntryToSave,
  agentMaxOutError, agentMaxOutLimit, storedReasoning, useAgentCapabilities,
  type AgentDraft,
} from "@/components/agent-llm-controls";
import { LocalSetupGuidePanel } from "@/components/local-setup-guide";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const PROVIDERS = [
  { id: "google", name: "Google Gemini", tokenUrl: "https://aistudio.google.com/app/apikey" },
  { id: "anthropic", name: "Anthropic (Claude)", tokenUrl: "https://console.anthropic.com/settings/keys" },
  { id: "openai", name: "OpenAI", tokenUrl: "https://platform.openai.com/api-keys" },
  { id: "openrouter", name: "OpenRouter", tokenUrl: "https://openrouter.ai/keys" },
  { id: "groq", name: "Groq", tokenUrl: "https://console.groq.com/keys" },
  { id: "mistral", name: "Mistral AI", tokenUrl: "https://console.mistral.ai/api-keys/" },
] as const;

const PROVIDER_OPTIONS = PROVIDERS.map((p) => ({ value: p.id, label: p.name }));

// Embeddings can only go where LiteLLM has an embeddings route. Its
// embedding() dispatch has no anthropic or groq branch at all, and OpenRouter
// serves no embedding models in its catalog — so those three stayed in this
// dropdown only to fail at index time, after the person who picked one had
// left the page. Chat/review/agent keep the full vendor list.
const EMBEDDING_PROVIDER_IDS: string[] = ["google", "openai", "mistral"];
const EMBEDDING_PROVIDER_OPTIONS = PROVIDER_OPTIONS.filter(
  (o) => EMBEDDING_PROVIDER_IDS.includes(o.value),
);

// Self-hosted (OpenAI-compatible) — the same slug the embeddings side already
// uses in src/config.py. Deliberately NOT in PROVIDERS: the keys card demands
// a key and links a vendor console, and a local server has neither.
const SELF_HOSTED = "openai_compatible";
// The keyless-local sentinel ("local-no-key") lives on the BACKEND: the test
// endpoint accepts a missing key for this provider, so the UI simply omits
// the field instead of inventing a placeholder credential.
// Chat, review and the agent planner may point at a self-hosted server (they
// share one LiteLLM path); embeddings are pinned by the operator in the
// server environment — see the info entry on that card.
//
// Its LABEL is `settings.llm.selfHostedOption` and not a literal here. Every
// dictionary already translates that parenthetical inside the instructions
// that tell you to pick this entry — "choose “Self-hosted (kompatibilní s
// OpenAI)”" — so an English option sat under fifteen sentences naming it in
// something else, and the sentence and the option now read the same key.

// The embeddings dropdown's version of the entry above. NOT a saveable
// provider — selecting it reveals the operator instructions (EMBEDDING_* env
// variables) instead, because indexing ships the customer's source code to
// the embedder, so where it runs is decided by whoever controls the server.
const SELF_HOSTED_INFO = "__self_hosted_info__";

// Review output languages — native names, no i18n needed.
const REVIEW_LANGS = [
  { value: "en", label: "English" }, { value: "uk", label: "Українська" },
  { value: "pl", label: "Polski" }, { value: "de", label: "Deutsch" },
  { value: "fr", label: "Français" }, { value: "es", label: "Español" },
  { value: "pt", label: "Português" }, { value: "it", label: "Italiano" },
  { value: "nl", label: "Nederlands" }, { value: "cs", label: "Čeština" },
  { value: "sk", label: "Slovenčina" }, { value: "ro", label: "Română" },
  { value: "hu", label: "Magyar" }, { value: "bg", label: "Български" },
  { value: "el", label: "Ελληνικά" }, { value: "tr", label: "Türkçe" },
  { value: "sv", label: "Svenska" }, { value: "no", label: "Norsk" },
  { value: "da", label: "Dansk" }, { value: "fi", label: "Suomi" },
  { value: "et", label: "Eesti" }, { value: "lv", label: "Latviešu" },
  { value: "lt", label: "Lietuvių" }, { value: "hr", label: "Hrvatski" },
  { value: "sr", label: "Srpski" }, { value: "ka", label: "ქართული" },
  { value: "he", label: "עברית" }, { value: "ar", label: "العربية" },
  { value: "hi", label: "हिन्दी" }, { value: "vi", label: "Tiếng Việt" },
  { value: "th", label: "ไทย" }, { value: "id", label: "Bahasa Indonesia" },
  { value: "ms", label: "Bahasa Melayu" }, { value: "ja", label: "日本語" },
  { value: "ko", label: "한국어" }, { value: "zh-CN", label: "简体中文" },
  { value: "zh-TW", label: "繁體中文" },
];

export default function LLMConfigPage() {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { data: session } = useSession();
  const wsMe = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const activeRole = wsMe.data?.workspaces.find((w) => w.id === wsMe.data?.active_id)?.role;
  // Keys are per-workspace now: a user manages the workspace they own/administer
  // (everyone owns their personal workspace) — or if they're a global admin.
  const isAdmin =
    Boolean(session?.isAdmin) || activeRole === "owner" || activeRole === "admin";
  const config = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<ZapIcon className="h-6 w-6" />}
        title={t("settings.llm.pageTitle")}
        description={
          <>
            {t("settings.llm.pageDescription")}{" "}
            <Link className="underline" href="/admin/agents">{t("settings.llm.aiAgentsLink")}</Link>.
          </>
        }
        tabs={<SectionTabs set="settings" />}
      />

      {config.isLoading && <div className="text-sm text-[var(--color-muted-foreground)]">{t("settings.llm.loading")}</div>}
      {!isAdmin && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-4 py-3 text-xs text-[var(--color-muted-foreground)]">
          {t("settings.llm.readOnlyNotice")}
        </div>
      )}

      {config.data && (
        <>
          {/* The self-hosted line used to live here, once, under the title,
              and scrolled you to the embeddings card. It now sits beside the
              provider select on every surface card instead — where the choice
              is actually made, and without a scroll to somewhere else. */}
          <GatewayModeBanner config={config.data} />
          <ProviderKeysCard config={config.data} isAdmin={isAdmin} onSaved={() => {
            void qc.invalidateQueries({ queryKey: ["llm-config"] });
            void qc.invalidateQueries({ queryKey: ["provider-models"] });
          }} />
          <ProfileCard isAdmin={isAdmin} surface="chat" icon={<MessagesSquareIcon className="h-4 w-4" />}
            title={t("settings.llm.chatTitle")} description={t("settings.llm.chatDescription")}
            config={config.data} />
          {/* Its own profile rather than borrowing chat's. Borrowing was
              wrong in both directions: a workspace that points chat at a
              strong, expensive model paid that for a JSON classification, and
              one that points chat at something cheap got a planner that
              misreads sentences. */}
          <ProfileCard isAdmin={isAdmin} surface="agent" icon={<WandIcon className="h-4 w-4" />}
            title={t("settings.llm.agentTitle")} description={t("settings.llm.agentDescription")}
            config={config.data} />
          <ProfileCard isAdmin={isAdmin} surface="review" icon={<GitPullRequestIcon className="h-4 w-4" />}
            title={t("settings.llm.reviewTitle")} description={t("settings.llm.reviewDescription")}
            config={config.data} />
          {/* Directly under the review card, because everything on it is a
              refinement of the model chosen there — each agent either inherits
              that model or replaces it for itself. */}
          <ReviewAgentsCard isAdmin={isAdmin} config={config.data} />
          <ProfileCard isAdmin={isAdmin} surface="embeddings" icon={<DatabaseIcon className="h-4 w-4" />}
            title={t("settings.llm.embeddingsTitle")}
            description={t("settings.llm.embeddingsDescription")}
            config={config.data} embeddings />
          <VectorStoreCard globalAdmin={Boolean(session?.isAdmin)} />
        </>
      )}
    </PageShell>
  );
}

// ─── Traffic mode (direct provider keys vs LiteLLM gateway) ─────────

/**
 * Which door LLM traffic leaves through. The keys on this page mean two
 * different things depending on the mode, so say which one is live.
 *
 * `gateway_enabled` is optional on the payload: an API that doesn't report it
 * is a direct-keys install, which is also the default.
 */
function GatewayModeBanner({ config }: { config: LLMConfig }) {
  const t = useT();
  const viaGateway = Boolean(
    (config as LLMConfig & { gateway_enabled?: boolean }).gateway_enabled,
  );
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-4 py-3 text-xs text-[var(--color-muted-foreground)]">
      <ServerIcon className="h-4 w-4 shrink-0" />
      <Badge variant={viaGateway ? "brand" : "outline"}>
        {viaGateway ? t("settings.llm.modeGateway") : t("settings.llm.modeDirect")}
      </Badge>
      <span>
        {viaGateway
          ? t("settings.llm.modeGatewayHint")
          : t("settings.llm.modeDirectHint")}
      </span>
    </div>
  );
}

// ─── Vector store (installation-level) ──────────────────────────────

function VectorStoreCard({ globalAdmin }: { globalAdmin: boolean }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const cfg = useQuery({
    queryKey: ["vector-store"],
    queryFn: () => vectorStoreApi.get(token!),
    enabled: !!token,
  });
  const [type, setType] = useState<string>("");
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const effType = type || cfg.data?.type || "local";

  const save = useMutation({
    mutationFn: () => vectorStoreApi.save(token!, { type: effType, url, api_key: key }),
    onSuccess: () => {
      toast.success(t("settings.vs.saved"));
      setKey("");
      void qc.invalidateQueries({ queryKey: ["vector-store"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const test = useMutation({
    mutationFn: () => vectorStoreApi.test(token!, { type: effType, url, api_key: key }),
    onSuccess: (r) => (r.ok ? toast.success(r.detail) : toast.error(r.detail)),
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <DatabaseIcon className="h-4 w-4" /> {t("settings.vs.title")}
        </CardTitle>
        <CardDescription>{t("settings.vs.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {cfg.data && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant="brand">{cfg.data.type}</Badge>
            {cfg.data.url && <code className="text-xs">{cfg.data.url}</code>}
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {t("settings.vs.source", { source: cfg.data.source })}
            </span>
          </div>
        )}
        {globalAdmin ? (
          <>
            <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
              <Select className="w-full" value={effType} onChange={(v) => setType(v)}
                options={[
                  { value: "local", label: t("settings.vs.local") },
                  { value: "qdrant", label: t("settings.vs.qdrant") },
                  { value: "pinecone", label: t("settings.vs.pinecone") },
                  { value: "weaviate", label: t("settings.vs.weaviate") },
                ]} />
              {effType === "qdrant" ? (
                <Input placeholder="https://xyz.cloud.qdrant.io  /  http://qdrant:6333"
                  value={url} onChange={(e) => setUrl(e.target.value)} />
              ) : <div className="flex items-center text-xs text-[var(--color-muted-foreground)]">
                    {effType === "local"
                      ? t("settings.vs.localHint")
                      : t("settings.vs.plannedHint")}
                  </div>}
            </div>
            {effType === "qdrant" && (
              <Input type="password" placeholder={t("settings.vs.keyPlaceholder")}
                value={key} onChange={(e) => setKey(e.target.value)} />
            )}
            <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={test.isPending}
                onClick={() => test.mutate()}>{t("settings.llm.testButton")}</Button>
              <Button size="sm" disabled={save.isPending}
                onClick={() => save.mutate()}>{t("settings.llm.saveButton")}</Button>
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("settings.vs.note")}</p>
          </>
        ) : (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("settings.vs.adminOnly")}</p>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Provider keys ───────────────────────────────────────────────────

function ProviderKeysCard({ config, isAdmin, onSaved }: { config: LLMConfig; isAdmin: boolean; onSaved: () => void }) {
  const token = useToken();
  const t = useT();
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [show, setShow] = useState<Record<string, boolean>>({});
  const statusOf = (p: string) => config.provider_keys.find((k) => k.provider === p);

  const save = useMutation({
    mutationFn: (provider: string) =>
      llmApi.saveConfig(token!, { provider_keys: { [provider]: keys[provider] } }),
    onSuccess: (_d, provider) => {
      toast.success(t("settings.llm.keySaved", { provider }));
      setKeys((k) => ({ ...k, [provider]: "" }));
      onSaved();
    },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const test = useMutation({
    mutationFn: (provider: string) =>
      llmApi.testConnection(token!, { provider, api_key: keys[provider] || "use-saved" }),
    onSuccess: (r) => r.ok ? toast.success(t("settings.llm.connected", { detail: r.detail }) + (r.models_available ? t("settings.llm.modelsCount", { count: r.models_available }) : "")) : toast.error(r.detail),
    onError: (e) => toast.error(t("settings.llm.testFailed", { message: (e as Error).message })),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><KeyIcon className="h-4 w-4" /> {t("settings.llm.providerKeysTitle")}</CardTitle>
        <CardDescription>{t("settings.llm.providerKeysDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {PROVIDERS.map((p) => {
          const st = statusOf(p.id);
          return (
            <div key={p.id} className="grid grid-cols-[140px_1fr_auto_auto] gap-2 items-center">
              <div className="text-sm flex items-center gap-1.5">
                {p.name}
                {st?.connected && <Badge variant="outline" className="text-[9px]">{st.source}</Badge>}
              </div>
              <div className="relative">
                <Input
                  type={show[p.id] ? "text" : "password"}
                  value={keys[p.id] ?? ""}
                  placeholder={st?.connected ? t("settings.llm.savedMasked", { masked: st.masked }) : t("settings.llm.pasteKey")}
                  disabled={!isAdmin}
                  onChange={(e) => setKeys((k) => ({ ...k, [p.id]: e.target.value }))}
                />
                <button type="button" onClick={() => setShow((s) => ({ ...s, [p.id]: !s[p.id] }))}
                  className="absolute right-2 top-1/2 -translate-y-1/2 opacity-60">
                  {show[p.id] ? <EyeOffIcon className="h-3.5 w-3.5" /> : <EyeIcon className="h-3.5 w-3.5" />}
                </button>
              </div>
              <Button size="sm" variant="outline" disabled={!isAdmin || !keys[p.id] || save.isPending} onClick={() => save.mutate(p.id)}>
                {t("settings.llm.saveButton")}
              </Button>
              <Button size="sm" variant="ghost" disabled={!isAdmin || test.isPending || (!keys[p.id] && !st?.connected)} onClick={() => test.mutate(p.id)}>
                {t("settings.llm.testButton")}
              </Button>
            </div>
          );
        })}
        <p className="text-[11px] text-[var(--color-muted-foreground)]">
          {t("settings.llm.getKey")}{" "}{PROVIDERS.map((p) => (
            <a key={p.id} className="underline mr-2" href={p.tokenUrl} target="_blank" rel="noreferrer">{p.id} ↗</a>
          ))}
        </p>
      </CardContent>
    </Card>
  );
}

// ─── One profile (chat / review / embeddings) ────────────────────────

function ProfileCard({
  surface, icon, title, description, config, embeddings, isAdmin,
}: {
  surface: LLMSurface;
  icon: React.ReactNode; title: string; description: string;
  config: LLMConfig; embeddings?: boolean; isAdmin: boolean;
}) {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const prof: LLMProfile = config.profiles[surface];
  const [provider, setProvider] = useState(prof.provider);
  const [model, setModel] = useState(prof.model);
  const [dims, setDims] = useState(String(prof.dimensions ?? 3072));
  const [baseUrl, setBaseUrl] = useState(prof.base_url ?? "");
  const [localKey, setLocalKey] = useState("");
  // Review card only: the model a failing agent retries on once the primary
  // is exhausted. "" = no fallback, which is also the backend's default.
  const [fallbackModel, setFallbackModel] = useState(config.review_fallback_model ?? "");

  useEffect(() => {
    setProvider(prof.provider); setModel(prof.model);
    setDims(String(prof.dimensions ?? 3072)); setBaseUrl(prof.base_url ?? "");
    setFallbackModel(config.review_fallback_model ?? "");
  }, [prof.provider, prof.model, prof.dimensions, prof.base_url, config.review_fallback_model]);

  // Chat, review and the agent planner share one LiteLLM path
  // (build_llm_client → resolve_profile → api_base), so all three may point
  // at a self-hosted server. Embeddings get the info entry below instead.
  const selfHostedAllowed = surface === "chat" || surface === "review" || surface === "agent";
  const isLocal = selfHostedAllowed && provider === SELF_HOSTED;
  // The embeddings info entry: not a provider, reveals the operator
  // instructions instead of a form — see SELF_HOSTED_INFO.
  const isEmbeddingsInfo = Boolean(embeddings) && provider === SELF_HOSTED_INFO;
  // One label for both entries, because they name one thing to the reader:
  // a model served from a machine you run. Only what picking it leads to
  // differs — a form on three cards, instructions on the fourth.
  const selfHostedLabel = t("settings.llm.selfHostedOption");
  const providerOptions = embeddings
    ? [...EMBEDDING_PROVIDER_OPTIONS,
       { value: SELF_HOSTED_INFO, label: selfHostedLabel }]
    : selfHostedAllowed
      ? [...PROVIDER_OPTIONS, { value: SELF_HOSTED, label: selfHostedLabel }]
      : PROVIDER_OPTIONS;
  // When the operator pinned embeddings in the server environment, the
  // editable profile below is not what runs — show the pinned one read-only
  // instead of a dropdown whose choice would silently be ignored.
  const eff = embeddings ? config.effective_embeddings ?? null : null;
  const envManaged = Boolean(eff);

  const keyConnected = config.provider_keys.find((k) => k.provider === provider)?.connected;
  const models = useQuery({
    // keyConnected is part of the key so saving a provider key automatically
    // refetches the model list (a keyless fetch caches an empty 200 otherwise).
    queryKey: ["provider-models", provider, keyConnected],
    queryFn: () => llmApi.providerModels(token!, provider),
    // Local model names are not in any catalog — a vendor /models call would
    // return nothing and its emptiness must not block this provider. The
    // embeddings info entry is not a vendor at all.
    enabled: !!token && !!provider && !isLocal && !isEmbeddingsInfo,
    staleTime: 5 * 60_000,
  });
  const options = embeddings ? (models.data?.embedding ?? []) : (models.data?.generation ?? []);

  const save = useMutation({
    mutationFn: () => llmApi.saveConfig(token!, {
      profiles: { [surface]: {
        provider, model,
        ...(embeddings ? { dimensions: Number(dims) } : {}),
        ...(isLocal ? { base_url: baseUrl.trim() } : {}),
      } },
      // The fallback rides the same save as the review model so the backend
      // can judge the pair together — "" clears it (empty = no fallback).
      ...(surface === "review" ? { review_fallback_model: fallbackModel.trim() } : {}),
      // An optional key typed on this card rides along in the same save.
      // Local servers rarely need one, so it has no row in the keys card.
      ...(isLocal && localKey.trim() ? { provider_keys: { [SELF_HOSTED]: localKey.trim() } } : {}),
    }),
    onSuccess: () => { toast.success(t("settings.llm.profileSaved", { title })); setLocalKey(""); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const localKeyStatus = config.provider_keys.find((k) => k.provider === SELF_HOSTED);
  const [localTest, setLocalTest] = useState<TestOutcome | null>(null);
  const testLocal = useMutation({
    mutationFn: () => llmApi.testConnection(token!, {
      provider: SELF_HOSTED,
      // A typed key wins; else the saved one; else none at all — keyless is
      // the normal local case and the endpoint accepts it for this provider.
      ...(localKey.trim()
        ? { api_key: localKey.trim() }
        : localKeyStatus?.connected ? { api_key: "use-saved" } : {}),
      base_url: baseUrl.trim(),
      model: model.trim() || null,
      // The endpoint knows two test shapes: generation ("chat" — review and
      // the agent ride the same probe) and embeddings. Sending the literal
      // surface name "review" or "agent" would be a 422.
      surface: "chat",
    }),
    onSuccess: (r) => setLocalTest({ result: r }),
    // The message is the backend's detail verbatim — for a blocked private
    // address that's the actionable EGRESS_ALLOW_PRIVATE_NETWORK explanation,
    // which a generic "failed" toast used to swallow.
    onError: (e) => setLocalTest({ error: (e as Error).message }),
  });
  const [effTest, setEffTest] = useState<TestOutcome | null>(null);
  const effKeyConnected = config.provider_keys.find((k) => k.provider === eff?.provider)?.connected;
  const testEffective = useMutation({
    mutationFn: () => llmApi.testConnection(token!, {
      provider: eff!.provider,
      // Keyless unless a key for this provider is saved — same reasoning as
      // the local test above; the env-pinned embedder is typically keyless.
      ...(effKeyConnected ? { api_key: "use-saved" } : {}),
      ...(eff!.base_url ? { base_url: eff!.base_url } : {}),
      model: eff!.model,
      surface: "embeddings",
    }),
    onSuccess: (r) => setEffTest({ result: r }),
    onError: (e) => setEffTest({ error: (e as Error).message }),
  });
  const engine = config.review_engine ?? "api";
  const saveEngine = useMutation({
    mutationFn: (next: "api" | "claude_code") =>
      llmApi.saveConfig(token!, { review_engine: next } as Record<string, unknown>),
    onSuccess: () => { toast.success(t("settings.llm.engineSaved")); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const isReview = surface === "review";
  const claudeEngine = isReview && engine === "claude_code";
  // Vault generation runs on the chat profile (get_gemini_client resolves
  // "chat"), so the documentation language belongs on this card and not with
  // review — which is a different audience anyway: PR comments are read by
  // outside contributors on GitHub, documentation by the team.
  const isChat = surface === "chat";
  const docsLanguage = config.docs_language ?? "uk";
  const docsEngine = config.docs_engine ?? "api";
  const saveDocsEngine = useMutation({
    mutationFn: (next: string) =>
      llmApi.saveConfig(token!, { docs_engine: next } as Record<string, unknown>),
    onSuccess: () => { toast.success(t("settings.llm.docsEngineSaved")); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const saveDocsLanguage = useMutation({
    mutationFn: (next: string) =>
      llmApi.saveConfig(token!, { docs_language: next } as Record<string, unknown>),
    onSuccess: () => { toast.success(t("settings.llm.docsLanguageSaved")); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const language = config.review_language ?? "en";
  const saveLanguage = useMutation({
    mutationFn: (next: string) =>
      llmApi.saveConfig(token!, { review_language: next } as Record<string, unknown>),
    onSuccess: () => { toast.success(t("settings.llm.languageSaved")); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });
  const reindex = useMutation({
    mutationFn: () => llmApi.reindexEmbeddings(token!),
    onSuccess: (r) => { toast.success(r.detail); qc.invalidateQueries({ queryKey: ["llm-config"] }); },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">{icon} {title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isReview && (
          <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
            <div>
              <Label>{t("settings.llm.engineLabel")}</Label>
              <Select className="w-full" value={engine} disabled={!isAdmin}
                onChange={(v) => saveEngine.mutate(v as "api" | "claude_code")}
                options={[
                  { value: "api", label: t("settings.llm.engineApi") },
                  { value: "claude_code", label: t("settings.llm.engineClaude") },
                ]} />
            </div>
            <div className="flex items-end pb-1 text-xs text-[var(--color-muted-foreground)]">
              {claudeEngine ? t("settings.llm.engineClaudeHint") : t("settings.llm.engineApiHint")}
            </div>
          </div>
        )}
        {isReview && (
          <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
            <div>
              <Label>{t("settings.llm.languageLabel")}</Label>
              <Select className="w-full" value={language} disabled={!isAdmin}
                onChange={(v) => saveLanguage.mutate(v)}
                options={REVIEW_LANGS} />
            </div>
            <div className="flex items-end pb-1 text-xs text-[var(--color-muted-foreground)]">
              {t("settings.llm.languageHint")}
            </div>
          </div>
        )}
        {isChat && (
          <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
            <div>
              <Label>{t("settings.llm.docsLanguageLabel")}</Label>
              <Select className="w-full" value={docsLanguage} disabled={!isAdmin}
                onChange={(v) => saveDocsLanguage.mutate(v)}
                options={REVIEW_LANGS} />
            </div>
            <div className="flex items-end pb-1 text-xs text-[var(--color-muted-foreground)]">
              {t("settings.llm.docsLanguageHint")}
            </div>
          </div>
        )}
        {isChat && (
          <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
            <div>
              <Label>{t("settings.llm.docsEngineLabel")}</Label>
              <Select className="w-full" value={docsEngine} disabled={!isAdmin}
                onChange={(v) => saveDocsEngine.mutate(v)}
                options={[
                  { value: "api", label: t("settings.llm.docsEngineApi") },
                  { value: "claude_code", label: t("settings.llm.docsEngineAgent") },
                ]} />
            </div>
            <div className="flex items-end pb-1 text-xs text-[var(--color-muted-foreground)]">
              {t("settings.llm.docsEngineHint")}
            </div>
          </div>
        )}
        {eff && (
          <div className="space-y-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant="outline">{t("settings.llm.effectiveEmbeddingsBadge")}</Badge>
              <Badge variant="brand">{eff.provider}</Badge>
              <code className="text-xs">{eff.model}</code>
              {eff.dimensions != null && (
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {t("settings.llm.dimensionsLabel")}: {eff.dimensions}
                </span>
              )}
            </div>
            {eff.base_url && (
              <div className="text-xs text-[var(--color-muted-foreground)]">
                {t("settings.llm.selfHostedBaseUrlLabel")}: <code>{eff.base_url}</code>
              </div>
            )}
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("settings.llm.effectiveEmbeddingsHint")}</p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" disabled={!isAdmin || testEffective.isPending}
                onClick={() => testEffective.mutate()}>
                {testEffective.isPending ? "…" : t("settings.llm.testConnection")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => reindex.mutate()} disabled={!isAdmin || reindex.isPending}>
                <RefreshCwIcon className="h-3.5 w-3.5 mr-1" /> {reindex.isPending ? "…" : t("settings.llm.reindexAll")}
              </Button>
            </div>
            <TestResultPanel outcome={effTest} />
          </div>
        )}
        {claudeEngine || envManaged ? null : (
        <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
          <div>
            <Label>{t("settings.llm.providerLabel")}</Label>
            <Select className="w-full" options={providerOptions}
              value={provider} disabled={!isAdmin} onChange={(v) => { setProvider(v); setModel(""); }} />
          </div>
          {isEmbeddingsInfo ? <div /> : (
          <div>
            <Label>{t("settings.llm.modelLabel")} {!isLocal && !keyConnected && <span className="text-amber-600">{t("settings.llm.addKeyHint")}</span>}</Label>
            {isLocal ? (
              // Free text, not the catalog dropdown: the names a local server
              // serves exist nowhere but on that server, and an empty vendor
              // /models list must not block choosing one.
              <Input value={model} disabled={!isAdmin}
                placeholder={t("settings.llm.selfHostedModelPlaceholder")}
                onChange={(e) => setModel(e.target.value)} />
            ) : (() => {
              const selectOptions = [
                ...(!options.includes(model) && model ? [{ value: model, label: t("settings.llm.currentModel", { model }) }] : []),
                ...options.map((m) => ({ value: m, label: m })),
              ];
              return (
                <Select className="w-full"
                  value={model} disabled={!isAdmin || selectOptions.length === 0}
                  onChange={(v) => setModel(v)}
                  placeholder={options.length ? t("settings.llm.selectModel") : (models.isLoading ? t("settings.llm.loadingModels") : t("settings.llm.noModels"))}
                  options={selectOptions} />
              );
            })()}
          </div>
          )}
        </div>
        )}
        {/* Directly under the review model, because it modifies that exact
            choice: when the primary is overloaded, a failing agent makes one
            more attempt on this model instead of dying. The hint states the
            trade in full — liveness for comparability — because a fallback
            that kicks in silently means two runs of one PR were judged by
            different models, and the operator must have chosen that. */}
        {isReview && !claudeEngine && (
          // `id` because the reviews page links here: a run whose agent was
          // taken over by the fallback, or whose reasoning level the provider
          // refused, names this control as the remedy — the link has to land
          // on it, not at the top of a long page.
          <div id="review-fallback" className="grid gap-3 sm:grid-cols-[1fr_2fr] scroll-mt-20">
            <div>
              <Label>{t("settings.llm.fallbackModelLabel")}</Label>
              {isLocal ? (
                <Input value={fallbackModel} disabled={!isAdmin}
                  placeholder={t("settings.llm.selfHostedModelPlaceholder")}
                  onChange={(e) => setFallbackModel(e.target.value)} />
              ) : (
                <Select className="w-full"
                  value={fallbackModel} disabled={!isAdmin}
                  onChange={(v) => setFallbackModel(v)}
                  options={[
                    { value: "", label: t("settings.llm.fallbackNone") },
                    ...(fallbackModel && fallbackModel !== model && !options.includes(fallbackModel)
                      ? [{ value: fallbackModel, label: t("settings.llm.currentModel", { model: fallbackModel }) }]
                      : []),
                    // The primary is filtered out because picking it is the
                    // one pointless choice — the backend refuses it too.
                    ...options.filter((m) => m !== model).map((m) => ({ value: m, label: m })),
                  ]} />
              )}
              {!!fallbackModel.trim() && fallbackModel.trim() === model.trim() && (
                <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                  {t("settings.llm.fallbackSameAsPrimary")}
                </p>
              )}
            </div>
            <div className="flex items-end pb-1 text-xs text-[var(--color-muted-foreground)]">
              {t("settings.llm.fallbackModelHint")}
            </div>
          </div>
        )}
        {isEmbeddingsInfo && (
          /* The WHY, on the page and not behind a toggle: indexing ships
             the customer's source code to the embedder, so the decision
             belongs to whoever controls the server. */
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("settings.llm.selfHostedEmbeddingsWhy")}
          </p>
        )}
        {/* Beside the provider select, on every card, whether or not a local
            server is already chosen: that models CAN run on your own hardware
            was previously discoverable only by opening a dropdown, and the
            people who need to know it are the ones who never opened it.

            The Claude Code engine is the one case without it — reviews then
            run on a subscription and there is no provider select above at
            all, so a line pointing at one would be pointing at nothing. */}
        {!claudeEngine && (
          <LocalSetupGuidePanel
            // Picking the embeddings info entry IS the request to read the
            // instructions, and it happens long after this panel mounted — so
            // the selection is the key, and choosing it hands back a panel
            // that starts open. Leaving the entry closes it again, which is
            // the same sentence read backwards.
            key={isEmbeddingsInfo ? "self-hosted-info" : "provider"}
            hint={embeddings
              ? t("settings.llm.selfHostedHintEmbeddings")
              : t("settings.llm.selfHostedHint", { option: selfHostedLabel })}
            defaultOpen={isEmbeddingsInfo}
          />
        )}
        {isLocal && !claudeEngine && (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <Label>{t("settings.llm.selfHostedBaseUrlLabel")}</Label>
                <Input placeholder="http://host.docker.internal:11434/v1"
                  value={baseUrl} disabled={!isAdmin}
                  onChange={(e) => setBaseUrl(e.target.value)} />
              </div>
              <div>
                <Label>{t("settings.llm.selfHostedKeyLabel")}</Label>
                <Input type="password" value={localKey} disabled={!isAdmin}
                  placeholder={localKeyStatus?.connected ? t("settings.llm.savedMasked", { masked: localKeyStatus.masked }) : ""}
                  onChange={(e) => setLocalKey(e.target.value)} />
                <p className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">{t("settings.llm.selfHostedKeyHint")}</p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline"
                disabled={!isAdmin || testLocal.isPending || !baseUrl.trim()}
                onClick={() => testLocal.mutate()}>
                {testLocal.isPending ? "…" : t("settings.llm.testConnection")}
              </Button>
            </div>
            <TestResultPanel outcome={localTest} />
          </>
        )}
        {embeddings && !envManaged && !isEmbeddingsInfo && (
          <div className="grid gap-3 sm:grid-cols-[1fr_auto] items-end">
            <div>
              <Label>{t("settings.llm.dimensionsLabel")}</Label>
              <Input type="number" min={128} max={3072} value={dims} disabled={!isAdmin} onChange={(e) => setDims(e.target.value)} />
            </div>
            <Button variant="outline" onClick={() => reindex.mutate()} disabled={!isAdmin || reindex.isPending}>
              <RefreshCwIcon className="h-3.5 w-3.5 mr-1" /> {reindex.isPending ? "…" : t("settings.llm.reindexAll")}
            </Button>
          </div>
        )}
        {embeddings && config.embeddings_reindex_needed && (
          <div className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
            <RefreshCwIcon className="h-3.5 w-3.5" /> {t("settings.llm.reindexNeeded")}
          </div>
        )}
        {/* The info entry is deliberately not saveable — there is nothing to
            save; the configuration happens in the server environment. */}
        {!claudeEngine && !envManaged && !isEmbeddingsInfo && (
          <div className="flex justify-end">
            <Button size="sm" onClick={() => save.mutate()}
              disabled={!isAdmin || save.isPending || !model || (isLocal && !baseUrl.trim())
                // A fallback equal to the primary retries the model that just
                // failed — the backend 422s it; dead button + red hint here.
                || (isReview && !!fallbackModel.trim() && fallbackModel.trim() === model.trim())}>
              <SaveIcon className="h-4 w-4 mr-1" /> {save.isPending ? "…" : t("settings.llm.saveButton")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Per-agent overrides for the review agents ───────────────────────
//
// The row itself, and every decision behind it, now live in
// web/components/agent-llm-controls.tsx — because the layer that WINS over
// this one, a repository's review policy, renders the same three controls and
// a second copy of them would have drifted. The vendor-prefix note that used
// to sit here moved with them; so did `reasoningToSave`, which is the one
// function in this feature that can delete a setting nobody touched.

/**
 * Model, output ceiling and reasoning, per review agent.
 *
 * WHY THE CEILING IS ON THIS SCREEN AT ALL: the architect agent (whose remit
 * the defect and contract agents now carry) failed in 43%
 * of runs against a 4096 ceiling, because in Gemini 3.x the thinking tokens
 * are drawn from the SAME output budget as the answer — 572 reasoning tokens
 * out of 613 output in one measurement. The findings array was cut mid-JSON
 * and the agent reported "no JSON array in the reply", which reads as a model
 * failure and is really a truncation. So the number is 16384, and lowering it
 * is how you reproduce that bug on purpose.
 *
 * All three settings also exist one layer down, per repository, on
 * /admin/review-policies/<slug> → Models. That layer wins over this one; this
 * is the workspace-wide default under it, and it is still the only layer that
 * can set anything at all for the compliance agent's MODEL (repo policy has no
 * column for it).
 */
function ReviewAgentsCard({ config, isAdmin }: { config: LLMConfig; isAdmin: boolean }) {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();

  // Serialised rather than compared by identity: the config object is new on
  // every refetch, and resyncing on identity would wipe what somebody is
  // typing here each time a neighbouring card saved something.
  const storedKey = JSON.stringify(config.agents ?? {});
  const [draft, setDraft] = useState<Record<ReviewAgent, AgentDraft>>(
    () => buildDraft(config.agents),
  );
  const [syncedKey, setSyncedKey] = useState(storedKey);
  if (syncedKey !== storedKey) {
    // React's documented "adjust state when a prop changes" — during render,
    // not in an effect, so the form never paints one frame of stale values.
    setSyncedKey(storedKey);
    setDraft(buildDraft(config.agents));
  }

  // The model each agent will actually call, as the STRING we ask about —
  // verbatim, whatever shape it is in. A typed override wins; otherwise
  // `effective_model`, which the server resolved through the whole chain and
  // already prefixed. Neither is touched on the way out (see the note at the
  // top of components/agent-llm-controls.tsx): a bare "gpt-4o" is a question
  // litellm can answer, and rewriting it into the review profile's vendor is
  // what turned a known model into an unrecognised one.
  //
  // `effective_model` describes what is SAVED, so between clearing an override
  // and pressing Save it still names the model being cleared. Refetching it
  // would mean asking the server to resolve a chain for a state that does not
  // exist yet; the value corrects itself on save, and until then the row says
  // which model it asked about rather than implying a different one.
  const effectiveModels = REVIEW_AGENTS.map((agent) =>
    draft[agent].model.trim()
    || config.agents?.[agent]?.effective_model
    || "",
  );

  // One capabilities lookup per agent, on that model — at CARD level, because
  // the Save button has to refuse a value above a ceiling before the row that
  // knows the ceiling has finished rendering.
  const caps = useAgentCapabilities(effectiveModels);

  /** Refused in the form, so the server never has to 422 it. */
  const maxOutError = (i: number, agent: ReviewAgent): "range" | "over" | null =>
    agentMaxOutError(draft[agent].maxOut, caps[i].caps);
  const blocked = REVIEW_AGENTS.some((agent, i) => maxOutError(i, agent) !== null);

  /** Whichever rows are about to save a reasoning value the operator cannot
   *  see or edit, because their capabilities lookup gave no answer and is not
   *  going to. Drives the note beside Save: the save is safe, and saying so
   *  is the point.
   *
   *  A lookup still in flight is excluded on purpose — it answers in a moment
   *  and listing it would only flash a callout in and out on every page load.
   *  What is left is the two states that stay: errored (`retry: false`, so it
   *  never comes back on its own) and no model to ask about. */
  const preservingReasoning = REVIEW_AGENTS.filter(
    (agent, i) =>
      caps[i].caps === null
      && !caps[i].loading
      && storedReasoning(config.agents?.[agent]) != null,
  );

  const save = useMutation({
    mutationFn: () => {
      const agents: Partial<Record<ReviewAgent, AgentLLMOverride>> = {};
      REVIEW_AGENTS.forEach((agent, i) => {
        const entry = agentEntryToSave(
          draft[agent], config.agents?.[agent], caps[i].caps,
        );
        if (entry) agents[agent] = entry;
      });
      return llmApi.saveConfig(token!, { agents });
    },
    onSuccess: () => {
      toast.success(t("settings.llm.agents.saved"));
      void qc.invalidateQueries({ queryKey: ["llm-config"] });
    },
    onError: (e) => toast.error(t("settings.llm.error", { message: (e as Error).message })),
  });

  // Reviews on the Claude Code engine are one subscription pass, not the
  // agent pipeline — the same reason the review card hides its model select.
  // The controls stay visible and go read-only: which agents exist and what
  // they were pointed at is information, and hiding it would only make the
  // operator wonder where the settings went.
  const claudeEngine = (config.review_engine ?? "api") === "claude_code";
  const editable = isAdmin && !claudeEngine;

  return (
    <Card id="review-agents">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BrainIcon className="h-4 w-4" /> {t("settings.llm.agents.title")}
        </CardTitle>
        <CardDescription>
          {t("settings.llm.agents.description")}{" "}
          {t("settings.llm.agents.repoPolicyNote")}{" "}
          <Link className="underline" href="/admin/review-policies">
            {t("settings.llm.agents.repoPolicyLink")}
          </Link>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* The whole reason this screen exists, in the place it gets read. */}
        <Callout tone="info">
          {t("settings.llm.agents.budgetNote", { tokens: DEFAULT_AGENT_MAX_OUTPUT })}
        </Callout>
        {claudeEngine && (
          <Callout tone="warning">{t("settings.llm.agents.claudeEngineNote")}</Callout>
        )}
        {REVIEW_AGENTS.map((agent, i) => (
          <AgentLLMRow
            key={agent}
            agent={agent}
            draft={draft[agent]}
            stored={config.agents?.[agent] ?? null}
            effective={config.agents?.[agent] ?? null}
            model={effectiveModels[i]}
            caps={caps[i].caps}
            loading={caps[i].loading}
            failed={caps[i].failed}
            limit={agentMaxOutLimit(caps[i].caps)}
            error={maxOutError(i, agent)}
            disabled={!editable}
            onChange={(patch) =>
              setDraft((prev) => ({ ...prev, [agent]: { ...prev[agent], ...patch } }))
            }
          />
        ))}
        {/* Save is deliberately NOT blocked by this: the value survives the
            press either way, and locking an operator out over a lookup that
            blipped costs more than it protects. The note exists so nobody has
            to guess what a read-only reasoning box is about to do. */}
        {editable && preservingReasoning.length > 0 && (
          <Callout tone="info">
            {t("settings.llm.agents.reasoningPreservedNote", {
              agents: preservingReasoning.join(", "),
            })}
          </Callout>
        )}
        <div className="flex items-center justify-end gap-3">
          {blocked && (
            <span className="text-xs text-red-600 dark:text-red-400">
              {t("settings.llm.agents.saveBlocked")}
            </span>
          )}
          <Button size="sm" onClick={() => save.mutate()}
            disabled={!editable || save.isPending || blocked}>
            <SaveIcon className="h-4 w-4 mr-1" /> {save.isPending ? "…" : t("settings.llm.saveButton")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** The six form rows from the stored map. Per-screen, because each layer
 *  stores its overrides in its own shape; `agentDraftFrom` is the shared half
 *  and knows that an empty string means inherit. */
function buildDraft(
  stored: Partial<Record<ReviewAgent, AgentSettings>> | undefined,
): Record<ReviewAgent, AgentDraft> {
  const out = {} as Record<ReviewAgent, AgentDraft>;
  for (const agent of REVIEW_AGENTS) out[agent] = agentDraftFrom(stored?.[agent]);
  return out;
}

// ─── Self-hosted helpers ─────────────────────────────────────────────

type TestOutcome = { result?: TestConnectionResult; error?: string };

/** The test outcome stays on the page rather than flashing through a toast:
 *  the egress explanation and a width mismatch are things you act on in a
 *  terminal, and a toast is gone before that terminal is even focused. */
function TestResultPanel({ outcome }: { outcome: TestOutcome | null }) {
  const t = useT();
  if (!outcome) return null;
  if (outcome.error) {
    return (
      <div className="whitespace-pre-wrap rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-400">
        {outcome.error}
      </div>
    );
  }
  const r = outcome.result!;
  return (
    <div className={cn(
      "space-y-1 rounded-md border px-3 py-2 text-xs",
      r.ok
        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
        : "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400",
    )}>
      {typeof r.vector_width === "number" && (
        <div className="text-sm font-semibold">
          {t("settings.llm.vectorWidth", { width: r.vector_width })}
        </div>
      )}
      <div className="whitespace-pre-wrap">
        {r.detail}
        {r.models_available ? t("settings.llm.modelsCount", { count: r.models_available }) : ""}
      </div>
      {r.warning && (
        <div className="whitespace-pre-wrap rounded-sm border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-400">
          {r.warning}
        </div>
      )}
    </div>
  );
}
