"use client";

/**
 * /onboarding — a guided first-run wizard done inline (no bouncing between
 * pages). Six steps, each reads real system state so it is always truthful
 * and re-visitable:
 *   1. Create your own workspace (the shared "default" one is read-only for
 *      keys, so a fresh admin needs a workspace they own).
 *   2. Connect an LLM provider — with a "Test" button that actually calls it.
 *   3. Connect a git account — with a "Verify" button that checks the token
 *      works and shows its scopes.
 *   4-5. Add + index a repo, then generate its vault (both happen on other
 *      pages — the "show me where" buttons spotlight the exact control).
 *   6. Optional: connect Claude Code — counts as done when connected OR
 *      explicitly skipped, so the progress bar can always reach 6/6.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangleIcon, ArrowRightIcon, BotIcon, BuildingIcon,
  CheckCircle2Icon, CircleIcon, CompassIcon, DatabaseIcon, FolderGit2Icon,
  KeyIcon, Loader2Icon, MapPinIcon, RocketIcon, ShieldCheckIcon, ZapIcon,
} from "lucide-react";

import {
  api, claudeApi, depsApi, llmApi, qaApi, workspacesApi,
  type AvailableRepo, type ConnectionStatus, type ConnectionVerifyResult,
  type RepoOut,
} from "@/lib/api";
import { FirstProofStep } from "@/components/first-proof-step";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { SPOTLIGHT_KEY } from "@/lib/tour";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const LLM_PROVIDERS = [
  { value: "google", label: "Google Gemini" },
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "openai", label: "OpenAI" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "groq", label: "Groq" },
  { value: "mistral", label: "Mistral AI" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "together_ai", label: "Together AI" },
];
const GIT_PROVIDERS = [
  { value: "github", label: "GitHub" },
  { value: "gitlab", label: "GitLab" },
  { value: "bitbucket", label: "Bitbucket" },
];

const slugify = (s: string) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

const CLAUDE_SKIP_KEY = "celmis:onboarding-claude-skipped";

export default function OnboardingPage() {
  const token = useToken();
  const t = useT();
  const router = useRouter();

  const ws = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const llm = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token,
  });
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: () => api<ConnectionStatus[]>("/api/connections", { token }),
    enabled: !!token,
  });

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  const qaRepos = useQuery({
    queryKey: ["qa-available-repos"],
    queryFn: () => qaApi.availableRepos(token!) as Promise<AvailableRepo[]>,
    enabled: !!token,
  });
  const claude = useQuery({
    queryKey: ["claude-connection"],
    queryFn: () => claudeApi.connection(token!),
    enabled: !!token,
  });

  const workspaces = ws.data?.workspaces ?? [];
  const active = workspaces.find((w) => w.id === ws.data?.active_id);
  const hasOwnWorkspace = workspaces.some((w) => w.slug !== "default");
  const keySet = (llm.data?.provider_keys ?? []).some((k) => k.connected);
  const gitConnected = (conns.data ?? []).some((c) => c.connected);
  const repoAdded = (repos.data ?? []).length > 0;
  const repoIndexed = (repos.data ?? []).some((r) => r.indexed);
  const vaultReady = (qaRepos.data ?? []).some((r) => r.is_ready);
  const claudeConnected = Boolean(claude.data?.personal || claude.data?.workspace);

  // Claude is optional: the step counts as done when connected OR explicitly
  // skipped. Skipping is persisted so the wizard stays 6/6 on revisits.
  const [claudeSkipped, setClaudeSkipped] = useState(false);
  useEffect(() => {
    try {
      setClaudeSkipped(localStorage.getItem(CLAUDE_SKIP_KEY) === "1");
    } catch { /* private mode etc. */ }
  }, []);
  const skipClaude = () => {
    try { localStorage.setItem(CLAUDE_SKIP_KEY, "1"); } catch {}
    setClaudeSkipped(true);
  };
  const claudeDone = claudeConnected || claudeSkipped;

  // Step 7 reads the same live state as the rest: it is done once an audit has
  // actually finished, not once the card has been looked at. Same query key as
  // the step's own component, so the two share one request.
  const depsRun = useQuery({
    queryKey: ["deps-latest-onboarding"],
    queryFn: () => depsApi.latest(token!),
    enabled: !!token,
  });
  // "done" is not "worked". The auditor writes done even when every
  // repository failed to clone, and step 7's own card says so — while the
  // header ticked complete and counted toward allDone above a card explaining
  // that nothing had been read.
  const proofScanned =
    ((depsRun.data?.summary as { repos_scanned?: number } | undefined)
      ?.repos_scanned ?? 0) > 0;
  const proofDone = depsRun.data?.status === "done" && proofScanned;

  const steps = [hasOwnWorkspace, keySet, gitConnected, repoIndexed, vaultReady,
                 claudeDone, proofDone];
  const doneCount = steps.filter(Boolean).length;
  const allDone = doneCount === steps.length;

  // "Show me where": stash the spotlight target (already translated) and
  // navigate — the destination page reads the key and highlights the control.
  const showWhere = (path: string, el: string, titleKey: string, descKey: string) => {
    try {
      sessionStorage.setItem(
        SPOTLIGHT_KEY,
        JSON.stringify({ el, title: t(titleKey), desc: t(descKey) }),
      );
    } catch { /* still navigate — worst case, no spotlight */ }
    router.push(path);
  };

  return (
    <div className="mx-auto w-full p-8 max-w-4xl space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <RocketIcon className="h-6 w-6" /> {t("onboarding.title")}
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          {t("onboarding.wizardSubtitle")}
        </p>
        <SectionTabs set="dashboard" />
        <div className="mt-3 flex items-center gap-3">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-[var(--color-muted)]">
            <div className="h-full rounded-full bg-[var(--color-brand)] transition-all"
                 style={{ width: `${(doneCount / steps.length) * 100}%` }} />
          </div>
          <span className="shrink-0 text-xs tabular-nums text-[var(--color-muted-foreground)]">
            {doneCount}/{steps.length}
          </span>
        </div>
      </div>

      {allDone && (
        <Card className="border-[var(--color-brand)]/40 bg-[var(--color-brand-muted)]">
          <CardContent className="flex items-center justify-between gap-4 pt-6">
            <div>
              <div className="font-medium">{t("onboarding.readyTitle")}</div>
              <div className="text-sm text-[var(--color-muted-foreground)]">
                {t("onboarding.readyBody")}
              </div>
            </div>
            <Link href="/projects">
              <Button>{t("onboarding.readyCta")} <ArrowRightIcon className="ml-1 h-4 w-4" /></Button>
            </Link>
          </CardContent>
        </Card>
      )}

      <WorkspaceStep done={hasOwnWorkspace} activeName={active?.name}
        activeSlug={active?.slug} onDone={() => ws.refetch()} />
      <LlmStep done={keySet} onDone={() => llm.refetch()} />
      <GitStep done={gitConnected} onDone={() => conns.refetch()} />

      <StepShell n={4} done={repoIndexed} icon={<FolderGit2Icon className="h-4 w-4" />}
        title={t("onboarding.repo.title")} body={t("onboarding.repo.body")}>
        <ol className="list-decimal space-y-1 pl-5 text-sm text-[var(--color-muted-foreground)]">
          <li>{t("onboarding.repo.tip1")}</li>
          <li>{t("onboarding.repo.tip2")}</li>
        </ol>
        <div className="flex flex-wrap items-center gap-2">
          <Link href="/repositories">
            <Button size="sm" variant={repoIndexed ? "outline" : "default"}>
              {repoAdded && !repoIndexed
                ? t("onboarding.repo.ctaIndex")
                : t("onboarding.repo.cta")}
              <ArrowRightIcon className="ml-1 h-3.5 w-3.5" />
            </Button>
          </Link>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              showWhere("/repositories", '[data-tour="add-repo"]',
                "tour.spot.addRepo.title", "tour.spot.addRepo.desc")}
          >
            <MapPinIcon className="mr-1 h-3.5 w-3.5" /> {t("onboarding.showWhere")}
          </Button>
        </div>
      </StepShell>

      <StepShell n={5} done={vaultReady} icon={<DatabaseIcon className="h-4 w-4" />}
        title={t("onboarding.vault.title")} body={t("onboarding.vault.body")}>
        <ol className="list-decimal space-y-1 pl-5 text-sm text-[var(--color-muted-foreground)]">
          <li>{t("onboarding.vault.tip1")}</li>
          <li>{t("onboarding.vault.tip2")}</li>
        </ol>
        <div className="flex flex-wrap items-center gap-2">
          <Link href="/repositories">
            <Button size="sm" variant={vaultReady ? "outline" : "default"} disabled={!repoIndexed}>
              {t("onboarding.vault.cta")} <ArrowRightIcon className="ml-1 h-3.5 w-3.5" />
            </Button>
          </Link>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              showWhere("/repositories", '[data-tour="generate-vault"]',
                "tour.spot.vault.title", "tour.spot.vault.desc")}
          >
            <MapPinIcon className="mr-1 h-3.5 w-3.5" /> {t("onboarding.showWhere")}
          </Button>
        </div>
      </StepShell>

      <StepShell n={6} done={claudeDone} icon={<BotIcon className="h-4 w-4" />}
        title={t("onboarding.claude.title")} body={t("onboarding.claude.body")}>
        <div className="flex flex-wrap items-center gap-2">
          <Link href="/claude">
            <Button size="sm" variant="outline">
              {t("onboarding.claude.cta")} <ArrowRightIcon className="ml-1 h-3.5 w-3.5" />
            </Button>
          </Link>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              showWhere("/claude", '[data-tour="claude-connect"]',
                "tour.spot.claude.title", "tour.spot.claude.desc")}
          >
            <MapPinIcon className="mr-1 h-3.5 w-3.5" /> {t("onboarding.showWhere")}
          </Button>
          {!claudeDone && (
            <Button size="sm" variant="ghost" onClick={skipClaude}>
              {t("onboarding.claude.skip")}
            </Button>
          )}
        </div>
      </StepShell>

      {/* Last, and the only one that shows rather than asks. Six steps of
          configuration is a long time to spend on a tool that has not yet
          demonstrated anything, so this one ends the wizard with a finding on
          screen and the SBOM beside it. */}
      <StepShell n={7} done={proofDone} icon={<ShieldCheckIcon className="h-4 w-4" />}
        title={t("onboarding.proof.title")} body={t("onboarding.proof.body")}>
        <FirstProofStep hasRepo={repoAdded} />
      </StepShell>

      {/* The 19 scenario cards moved to their own /capabilities reference. */}
      <Link href="/capabilities" className="block">
        <Card className="transition-colors hover:bg-[var(--color-accent)]/40">
          <CardContent className="flex items-center justify-between gap-4 pt-6">
            <div className="flex items-start gap-3">
              <CompassIcon className="mt-0.5 h-5 w-5 shrink-0 text-[var(--color-brand)]" />
              <div>
                <div className="font-medium">{t("onboarding.capabilitiesLink")}</div>
                <div className="text-sm text-[var(--color-muted-foreground)]">
                  {t("onboarding.capabilitiesLinkDesc")}
                </div>
              </div>
            </div>
            <ArrowRightIcon className="h-4 w-4 shrink-0 text-[var(--color-muted-foreground)]" />
          </CardContent>
        </Card>
      </Link>
    </div>
  );
}

function StepShell({
  n, done, icon, title, body, children,
}: {
  n: number; done: boolean; icon: React.ReactNode;
  title: string; body: string; children?: React.ReactNode;
}) {
  const t = useT();
  return (
    <Card className={done ? "opacity-80" : ""}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {done
            ? <CheckCircle2Icon className="h-5 w-5 text-[var(--color-brand)]" />
            : <CircleIcon className="h-5 w-5 text-[var(--color-muted-foreground)]" />}
          <span className="flex items-center gap-1.5">
            <span className="text-[var(--color-muted-foreground)]">{n}.</span> {icon} {title}
          </span>
          {done && <Badge variant="outline" className="ml-1 text-[9px]">{t("onboarding.done")}</Badge>}
        </CardTitle>
        <CardDescription>{body}</CardDescription>
      </CardHeader>
      {children && <CardContent className="space-y-3">{children}</CardContent>}
    </Card>
  );
}

function WorkspaceStep({
  done, activeName, activeSlug, onDone,
}: {
  done: boolean; activeName?: string; activeSlug?: string; onDone: () => void;
}) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () => workspacesApi.create(token!, name.trim(), slugify(name), ""),
    onSuccess: () => {
      toast.success(t("onboarding.ws.created"));
      setName("");
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      onDone();
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <StepShell n={1} done={done} icon={<BuildingIcon className="h-4 w-4" />}
      title={t("onboarding.ws.title")} body={t("onboarding.ws.body")}>
      {activeName && (
        <div className="text-xs text-[var(--color-muted-foreground)]">
          {t("onboarding.ws.current")}: <span className="font-medium text-[var(--color-foreground)]">{activeName}</span>
          {activeSlug === "default" && <> · <span className="text-amber-600 dark:text-amber-400">{t("onboarding.ws.defaultWarn")}</span></>}
        </div>
      )}
      {!done && (
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Label>{t("onboarding.ws.nameLabel")}</Label>
            <Input value={name} placeholder={t("onboarding.ws.namePlaceholder")} onChange={(e) => setName(e.target.value)} />
          </div>
          <Button disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <Loader2Icon className="h-4 w-4 animate-spin" /> : t("onboarding.ws.createBtn")}
          </Button>
        </div>
      )}
    </StepShell>
  );
}

function LlmStep({ done, onDone }: { done: boolean; onDone: () => void }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [provider, setProvider] = useState("google");
  const [key, setKey] = useState("");
  const [tested, setTested] = useState<{ ok: boolean; detail: string } | null>(null);

  const test = useMutation({
    mutationFn: () => llmApi.testConnection(token!, { provider, api_key: key.trim() }),
    onSuccess: (r) => setTested({ ok: r.ok, detail: r.detail || (r.ok ? "OK" : "failed") }),
    onError: (e) => setTested({ ok: false, detail: (e as Error).message }),
  });
  const save = useMutation({
    mutationFn: () => llmApi.saveConfig(token!, { provider_keys: { [provider]: key.trim() } }),
    onSuccess: () => {
      toast.success(t("onboarding.llm.saved"));
      qc.invalidateQueries({ queryKey: ["llm-config"] });
      onDone();
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <StepShell n={2} done={done} icon={<ZapIcon className="h-4 w-4" />}
      title={t("onboarding.llm.title")} body={t("onboarding.llm.body")}>
      {!done && (
        <>
          <div className="grid gap-3 sm:grid-cols-[auto_1fr]">
            <div className="flex flex-col gap-1.5">
              <Label>{t("onboarding.llm.provider")}</Label>
              <Select value={provider} options={LLM_PROVIDERS} className="min-w-44"
                onChange={(v) => { setProvider(v); setTested(null); }} />
            </div>
            <div>
              <Label>{t("onboarding.llm.keyLabel")}</Label>
              <Input type="password" value={key} placeholder="sk-..." autoComplete="off"
                onChange={(e) => { setKey(e.target.value); setTested(null); }} />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={!key.trim() || test.isPending}
              onClick={() => test.mutate()}>
              {test.isPending ? <Loader2Icon className="h-4 w-4 animate-spin" /> : t("onboarding.llm.test")}
            </Button>
            <Button size="sm" disabled={!key.trim() || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? <Loader2Icon className="h-4 w-4 animate-spin" /> : t("onboarding.llm.save")}
            </Button>
            {tested && (
              <span className={`text-xs ${tested.ok ? "text-[var(--color-brand)]" : "text-red-600 dark:text-red-400"}`}>
                {tested.ok ? "✓ " : "✗ "}{tested.detail}
              </span>
            )}
          </div>
        </>
      )}
    </StepShell>
  );
}

function GitStep({ done, onDone }: { done: boolean; onDone: () => void }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [provider, setProvider] = useState("github");
  const [tok, setTok] = useState("");
  const [email, setEmail] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [result, setResult] = useState<ConnectionVerifyResult | null>(null);

  const verify = useMutation({
    mutationFn: () =>
      api<ConnectionVerifyResult>(`/api/connections/${provider}`, {
        token, method: "PUT",
        json: {
          provider, token: tok.trim(),
          email: provider === "bitbucket" ? email.trim() || null : null,
          workspace: provider === "bitbucket" ? workspace.trim() || null : null,
        },
      }),
    onSuccess: (r) => {
      setResult(r);
      if (r.ok) {
        toast.success(t("onboarding.git.connected"));
        qc.invalidateQueries({ queryKey: ["connections"] });
        onDone();
      }
    },
    onError: (e) => setResult({ ok: false, provider, error: (e as Error).message }),
  });

  return (
    <StepShell n={3} done={done} icon={<KeyIcon className="h-4 w-4" />}
      title={t("onboarding.git.title")} body={t("onboarding.git.body")}>
      {!done && (
        <>
          <div className="grid gap-3 sm:grid-cols-[auto_1fr]">
            <div className="flex flex-col gap-1.5">
              <Label>{t("onboarding.git.provider")}</Label>
              <Select value={provider} options={GIT_PROVIDERS} className="min-w-44"
                onChange={(v) => { setProvider(v); setResult(null); }} />
            </div>
            <div>
              <Label>{t("onboarding.git.tokenLabel")}</Label>
              <Input type="password" value={tok} placeholder="ghp_... / glpat-..." autoComplete="off"
                onChange={(e) => { setTok(e.target.value); setResult(null); }} />
            </div>
          </div>
          {provider === "bitbucket" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div><Label>{t("onboarding.git.email")}</Label>
                <Input value={email} onChange={(e) => setEmail(e.target.value)} /></div>
              <div><Label>{t("onboarding.git.workspace")}</Label>
                <Input value={workspace} onChange={(e) => setWorkspace(e.target.value)} /></div>
            </div>
          )}
          <Button size="sm" disabled={!tok.trim() || verify.isPending} onClick={() => verify.mutate()}>
            {verify.isPending ? <Loader2Icon className="h-4 w-4 animate-spin" /> : <ShieldCheckIcon className="mr-1 h-4 w-4" />}
            {t("onboarding.git.verify")}
          </Button>

          {result && !result.ok && (
            <div className="flex items-start gap-1.5 text-xs text-red-600 dark:text-red-400">
              <AlertTriangleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{result.error || t("onboarding.git.failed")}</span>
            </div>
          )}
          {result?.ok && (
            <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3 text-xs">
              <div className="font-medium text-[var(--color-brand)]">✓ {result.username}</div>
              {result.scopes && result.scopes.length > 0 ? (
                <div className="mt-1">
                  <span className="text-[var(--color-muted-foreground)]">{t("onboarding.git.scopes")}: </span>
                  {result.scopes.map((s) => (
                    <Badge key={s} variant="outline" className="ml-1 text-[9px]">{s}</Badge>
                  ))}
                  {provider === "github" && !result.scopes.includes("repo") && (
                    <div className="mt-1 text-amber-600 dark:text-amber-400">{t("onboarding.git.scopeWarn")}</div>
                  )}
                </div>
              ) : (
                <div className="mt-1 text-[var(--color-muted-foreground)]">{t("onboarding.git.noScopes")}</div>
              )}
            </div>
          )}
        </>
      )}
    </StepShell>
  );
}
