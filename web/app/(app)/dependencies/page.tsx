"use client";

/**
 * /dependencies — workspace-wide library version + vulnerability audit.
 *
 * Deterministic data (registries + OSV.dev), then a per-repo "Fix with
 * Claude" hand-off that pre-fills an agent session with the exact updates.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  BotIcon, CircleSlashIcon, DownloadIcon, EyeOffIcon, FileTextIcon, GaugeIcon, GitBranchIcon, InfoIcon, LayersIcon, PackageSearchIcon, PlayIcon, PrinterIcon, RefreshCwIcon,
  ShieldAlertIcon, ShieldCheckIcon, WrenchIcon,
} from "lucide-react";

import {
  api, API_BASE, depsApi, downloadWithAuth, projectsApi, requestHeaders,
  type DepFinding, type DepVuln, type HygieneItem as ApiHygieneItem,
  type ProjectOut, type RepoDeveloperItem, type RepoOut,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { HelpButton } from "@/components/ui/help-button";
import { Callout } from "@/components/ui/callout";
import { Tooltip } from "@/components/ui/tooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageShell } from "@/components/page-shell";
import { InlineHelp } from "@/components/ui/inline-help";
import { ComplianceArtifacts } from "@/components/compliance-artifacts";
import { SectionTabs } from "@/components/section-tabs";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { RepoBranchTable } from "@/components/repo-branch-table";
import { CountUp } from "@/components/ui/count-up";
import { EmptyState } from "@/components/ui/empty-state";
import { WorkspaceBadge } from "@/components/workspace-badge";

const SEV_VARIANT: Record<string, "default" | "brand" | "destructive"> = {
  low: "default", medium: "brand", high: "destructive", critical: "destructive",
};

type Translate = (key: string, vars?: Record<string, string | number>) => string;

/**
 * The message catalogs are maintained outside this file and the keys for the
 * branch controls do not exist there yet — `t()` echoes an unknown key
 * straight back, which would ship literal "deps.branchNote" as UI text.
 * English stands in until the keys land, then they take over untouched.
 */
function withFallback(t: Translate) {
  return (key: string, english: string, vars?: Record<string, string | number>) => {
    const translated = t(key, vars);
    if (translated !== key) return translated;
    return english.replace(/\{(\w+)\}/g, (_m, name: string) =>
      vars && name in vars ? String(vars[name]) : `{${name}}`);
  };
}

/**
 * The audit now carries provenance. Native auditors (pip-audit, npm audit,
 * govulncheck, …) resolve the FULL dependency tree, so they see transitive
 * packages that manifest scanning never could; OSV remains the fallback for
 * ecosystems whose tool is not in the image. Both facts travel inside the
 * existing JSONB payloads (`vulns[]`, `summary`), which `lib/api.ts` types
 * predate — so they are read through narrow local views rather than by
 * widening a shared type other pages depend on.
 */
type VulnMeta = {
  /** pip-audit | npm-audit | pnpm-audit | yarn-audit | govulncheck | … | osv */
  source?: string;
  transitive?: boolean;
  is_dev?: boolean;
  subproject?: string;
};

type NativeCheck = {
  repo?: string;
  ecosystem: string;
  tool: string;
  subproject: string;
  /** checked — the tool ran; not_checked — it isn't installed / no lock file;
   *  failed — it ran but produced nothing we could read; partial — it saw the
   *  direct dependencies but not the transitive tree. */
  status: string;
  reason?: string;
  findings?: number;
};

/** Declared once, in api.ts.
 *
 *  It was declared twice, and the copy here silently lacked `line` and
 *  `excerpt` — so the auditor extracted the evidence, the API returned it, and
 *  the page dropped it on the floor with nothing failing anywhere. */
type HygieneItem = ApiHygieneItem;

type DepsSummaryExtra = {
  sources?: Record<string, number>;
  native_enabled?: boolean;
  native_tools?: Record<string, boolean>;
  native_checks?: NativeCheck[];
  not_checked?: NativeCheck[];
  transitive?: number;
  transitive_vulnerable?: number;
  /** Pure version distance, counted independently of vulnerabilities. */
  drift?: Record<string, number>;
  /** What to do — the same buckets the recommendation column uses. */
  groups?: Record<string, number>;
  hygiene?: {
    total?: number;
    by_kind?: Record<string, number>;
    items?: HygieneItem[];
  };
};

const HYGIENE_KINDS = ["lock_drift", "install_script", "non_registry", "suspect_name"] as const;

const HYGIENE_TONE: Record<string, "default" | "brand" | "destructive"> = {
  low: "default", medium: "brand", high: "destructive",
};

function vulnMeta(v: DepVuln): DepVuln & VulnMeta {
  return v as DepVuln & VulnMeta;
}

/** Distinct advisory sources behind a row — "who says so", in one glance. */
function findingSources(f: DepFinding): string[] {
  return Array.from(new Set(f.vulns.map((v) => vulnMeta(v).source || "osv")));
}

/** Minutes without a progress write after which a run is treated as stuck.
 * The auditor heartbeats every ~20 registry lookups / every OSV chunk, so a
 * healthy run touches `updated_at` far more often than this. */
const STUCK_MINUTES = 3;

/** "4:07" / "0:12" — locale-neutral elapsed time. */
function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * What the audit runs over. Lives in the page header (not behind a "Scope"
 * button) because a hidden scope is the same bug as a wrong scope: the user
 * cannot tell which repos the numbers came from.
 */
type ScopeMode = "all" | "project" | "repos" | "owner" | "developer";

type AuditScope = NonNullable<Parameters<typeof depsApi.startAudit>[1]>;

const SCOPE_STORAGE_KEY = "celmis:deps:scope";

/** How many scoped repos are asked for their branch list.
 *
 *  One provider API call each, on a page the user is only passing through, so
 *  this is a suggestion list rather than a census. A team's branch names repeat
 *  across its repos — the first few name them all. */
const BRANCH_PROBE_REPOS = 6;

type StoredScope = {
  mode: ScopeMode;
  projectId: string;
  owner: string;
  developer: string[];
  repos: string[];
};

function loadScope(): StoredScope | null {
  try {
    const raw = localStorage.getItem(SCOPE_STORAGE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as Partial<StoredScope>;
    const mode: ScopeMode =
      p.mode === "project" || p.mode === "repos" || p.mode === "owner"
      || p.mode === "developer" ? p.mode : "all";
    return {
      mode,
      projectId: typeof p.projectId === "string" ? p.projectId : "",
      owner: typeof p.owner === "string" ? p.owner : "",
      // Was a single string before multi-select — an old stored scope must
      // not throw the page, it just carries over as a one-person list.
      developer: Array.isArray(p.developer)
        ? p.developer.filter((d) => typeof d === "string")
        : typeof p.developer === "string" && p.developer ? [p.developer] : [],
      repos: Array.isArray(p.repos) ? p.repos.filter((r) => typeof r === "string") : [],
    };
  } catch {
    return null;
  }
}

/** Compare two version strings numerically, segment by segment.
 *
 *  Deliberately small: it only has to order the `fixed_in` values of ONE
 *  package's advisories, which come from the same registry and share a shape.
 *  A lexical compare would put "0.9.0" above "0.31.1" and hand an agent a
 *  target that fixes less than the one it replaced.
 *
 *  A pre-release suffix sorts below the same release ("1.2.0-rc1" < "1.2.0"),
 *  which is the safe direction: preferring the release is never wrong. */
function compareVersions(a: string, b: string): number {
  const parts = (v: string) =>
    v.replace(/^[^0-9]*/, "").split(/[.+-]/).map((x) => {
      const n = Number.parseInt(x, 10);
      return Number.isNaN(n) ? -1 : n;
    });
  const pa = parts(a);
  const pb = parts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

export default function DependenciesPage() {
  const t = useT();
  const tf = withFallback(t);
  const token = useToken();
  const qc = useQueryClient();
  const router = useRouter();
  const [filter, setFilter] = useState<"all" | "vulnerable" | "outdated">("all");
  const [repoFilter, setRepoFilter] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [autoReport, setAutoReport] = useState<"none" | "api" | "claude_code">("none");
  const [scopeMode, setScopeMode] = useState<ScopeMode>("all");
  const [scopeProjectId, setScopeProjectId] = useState("");
  const [ownerScope, setOwnerScope] = useState("");
  // Who writes the code, as opposed to whose account it sits under. Read from
  // the ownership snapshots the intel builder writes, so it is real git
  // authorship rather than an organisation name.
  // Several people, not one: a change usually spans a team, and the same
  // person can still arrive under more than one git identity.
  const [developerScope, setDeveloperScope] = useState<string[]>([]);
  // Escape hatch for a branch none of the scoped repos has. Off by default —
  // the list is built from the repos in scope, so it is normally complete.
  const [typeBranch, setTypeBranch] = useState(false);
  const [repoScope, setRepoScope] = useState<Set<string>>(new Set());
  // Deliberately NOT persisted with the rest of the scope: a branch override
  // that survives a reload would keep auditing `hotfix/x` weeks later while
  // the card says "this run only" — the same invisible-scope bug the picker
  // above exists to prevent.
  const [branchOverride, setBranchOverride] = useState("");
  // Nothing is persisted until the stored scope has been read back, otherwise
  // the first render's empty defaults would overwrite it.
  const [scopeRestored, setScopeRestored] = useState(false);

  useEffect(() => {
    const saved = loadScope();
    if (saved) {
      setScopeMode(saved.mode);
      setScopeProjectId(saved.projectId);
      setOwnerScope(saved.owner);
      setDeveloperScope(saved.developer);
      setRepoScope(new Set(saved.repos));
    }
    setScopeRestored(true);
  }, []);

  useEffect(() => {
    if (!scopeRestored) return;
    try {
      localStorage.setItem(SCOPE_STORAGE_KEY, JSON.stringify({
        mode: scopeMode, projectId: scopeProjectId,
        owner: ownerScope, developer: developerScope, repos: Array.from(repoScope),
      } satisfies StoredScope));
    } catch { /* private mode / quota — the scope just won't persist */ }
  }, [scopeRestored, scopeMode, scopeProjectId, ownerScope, developerScope, repoScope]);

  const registered = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => projectsApi.list(token!),
    enabled: !!token,
  });

  // Report generation blocks its own request for minutes — a Claude Code
  // session start to finish — and the server SAVES the result onto the run
  // before replying. So the poll is not decoration: if the POST dies at a
  // proxy or the tab is reloaded, this is what makes the finished report
  // appear anyway.
  const [reportStartedAt, setReportStartedAt] = useState<number | null>(null);
  const [reportElapsed, setReportElapsed] = useState(0);
  useEffect(() => {
    if (reportStartedAt === null) { setReportElapsed(0); return; }
    const tick = () =>
      setReportElapsed(Math.round((Date.now() - reportStartedAt) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [reportStartedAt]);

  const run = useQuery({
    queryKey: ["deps-latest"],
    queryFn: () => depsApi.latest(token!),
    enabled: !!token,
    refetchInterval: (q) =>
      q.state.data?.status === "running" || q.state.data?.status === "queued"
        ? 4000
        : reportStartedAt !== null ? 10_000 : false,
  });
  const findings = useQuery({
    queryKey: ["deps-findings", run.data?.id, filter],
    queryFn: () =>
      depsApi.findings(token!, run.data!.id, filter === "all" ? undefined : filter),
    enabled: !!token && !!run.data?.id && run.data.status === "done",
  });

  // ── Scope → the two fields the API actually understands (repo_slugs+owner).
  // A project is just its repo list, expanded here.
  const selectedProject: ProjectOut | undefined =
    (projects.data ?? []).find((p) => p.id === scopeProjectId);
  const projectSlugs = selectedProject?.repos.map((r) => r.repo_slug) ?? [];
  const developers = useQuery({
    queryKey: ["repo-developers"],
    queryFn: () => api<RepoDeveloperItem[]>("/api/repos/developers", { token }),
    enabled: !!token,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const developerRepos = Array.from(new Set(
    (developers.data ?? [])
      .filter((d) => developerScope.includes(d.identity))
      .flatMap((d) => d.repos),
  ));

  const scopeSlugs: string[] | undefined =
    scopeMode === "project"
      ? projectSlugs
      : scopeMode === "repos" && repoScope.size
        ? Array.from(repoScope)
        // A developer resolves to explicit slugs, not to a filter the backend
        // would have to reimplement: ownership lives in a snapshot the API
        // already computed, and sending the result keeps one source of truth.
        : scopeMode === "developer" && developerRepos.length
          ? developerRepos
          : undefined;
  const scopeOwner = scopeMode === "owner" ? ownerScope.trim() || undefined : undefined;
  // A narrowing scope that resolves to "nothing" would fall through to the
  // whole workspace on the backend (`if repo_slugs:` / `if owner:`), i.e. the
  // opposite of what the user picked — block the run instead.
  const scopeIncomplete =
    (scopeMode === "project" && projectSlugs.length === 0) ||
    (scopeMode === "repos" && repoScope.size === 0) ||
    (scopeMode === "owner" && !scopeOwner) ||
    (scopeMode === "developer" && developerRepos.length === 0);
  // How many repos the run will actually touch — shown next to the picker so
  // "0" can never be a silent surprise after the audit finishes.
  const allRepos = registered.data ?? [];
  // Owners of REGISTERED repos, not of everything the provider token can see:
  // an audit only ever covers repos registered here, so offering an owner with
  // none of them would offer a scope that selects nothing. Every entry below
  // is guaranteed to match at least one repo.
  const ownerOptions = (() => {
    const counts = new Map<string, number>();
    for (const r of allRepos) {
      const i = r.full_name.lastIndexOf("/");
      if (i < 0) continue;
      const owner = r.full_name.slice(0, i);
      counts.set(owner, (counts.get(owner) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([owner, n]) => ({ value: owner, label: owner, hint: String(n) }));
  })();
  const scopeCount =
    scopeMode === "all" ? allRepos.length
      : scopeMode === "project" ? projectSlugs.length
        : scopeMode === "repos" ? repoScope.size
          : scopeMode === "developer" ? developerRepos.length
            : scopeOwner
            ? allRepos.filter((r) =>
              r.full_name.toLowerCase().startsWith(`${scopeOwner.toLowerCase().replace(/\/+$/, "")}/`)).length
            : allRepos.length;
  // The count answers "how many repos"; this answers "which ones, at which
  // branch" — and a wrong branch is exactly how a fixed vulnerability keeps
  // showing up in the findings.
  const scopedRepos: RepoOut[] =
    scopeMode === "project"
      ? allRepos.filter((r) => projectSlugs.includes(r.slug))
      : scopeMode === "developer"
        ? allRepos.filter((r) => developerRepos.includes(r.slug))
        : scopeMode === "repos"
          ? allRepos.filter((r) => repoScope.has(r.slug))
        : scopeMode === "owner"
          ? (scopeOwner
            ? allRepos.filter((r) => r.full_name.toLowerCase()
              .startsWith(`${scopeOwner.toLowerCase().replace(/\/+$/, "")}/`))
            : allRepos)
          : allRepos;
  const branchForRun = branchOverride.trim();

  // Branch names the scoped repos actually have. Typing a branch that exists
  // in none of them is silently a no-op today — every repo falls back to its
  // own ref and the run reads something other than what the field says.
  //
  // Capped at BRANCH_PROBE_REPOS: each entry is one provider API call, and a
  // union built from the first few repos already names every branch a team
  // uses. The count on each option says how many of the probed repos have it,
  // so a branch that exists in one repo of eight is visibly that.
  const probed = scopedRepos.slice(0, BRANCH_PROBE_REPOS);
  const branchQueries = useQueries({
    queries: probed.map((r) => ({
      queryKey: ["repo-branches", r.slug],
      queryFn: () => api<string[]>(`/api/repos/${r.slug}/branches`, { token }),
      enabled: !!token,
      staleTime: 5 * 60_000,
      retry: false,
    })),
  });
  const branchOptions = (() => {
    const counts = new Map<string, number>();
    for (const q of branchQueries) {
      for (const b of q.data ?? []) counts.set(b, (counts.get(b) ?? 0) + 1);
    }
    const probedCount = probed.length;
    return [...counts.entries()]
      // Branches every probed repo shares come first: those are the ones an
      // override can be applied to without silently missing repos.
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([name, n]) => ({
        value: name,
        label: name,
        hint: n === probedCount ? undefined : `${n}/${probedCount}`,
      }));
  })();
  const branchesLoading = branchQueries.some((q) => q.isLoading);

  const start = useMutation({
    mutationFn: (opts: { force?: boolean } = {}) => {
      // `branch` is not part of the shared audit-scope type yet — the API
      // model still has to grow the field. Sending it only when the user set
      // one keeps today's backend happy (it drops unknown keys) instead of
      // failing every run for a feature nobody asked for.
      const scope: AuditScope & { branch?: string } = {
        owner: scopeOwner,
        repo_slugs: scopeSlugs,
        report_engine: autoReport,
        force: opts.force,
      };
      if (branchForRun) scope.branch = branchForRun;
      return depsApi.startAudit(token!, scope);
    },
    onSuccess: () => {
      toast.success(t("deps.auditStarted"));
      void qc.invalidateQueries({ queryKey: ["deps-latest"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const cancel = useMutation({
    mutationFn: () => depsApi.cancel(token!, run.data!.id),
    onSuccess: () => {
      toast.success(t("deps.cancelled"));
      void qc.invalidateQueries({ queryKey: ["deps-latest"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const [reportEngine, setReportEngine] = useState<"api" | "claude_code">("api");
  const [reportTemp, setReportTemp] = useState("0.2");
  // `|| 0.2` would silently rewrite a deliberate 0 — and 0 is exactly what the
  // field's hint recommends for a strictly factual report.
  const parsedTemp = Number(reportTemp);
  const temperature =
    reportTemp.trim() === "" || Number.isNaN(parsedTemp)
      ? 0.2
      : Math.min(2, Math.max(0, parsedTemp));
  const report = useMutation({
    mutationFn: () =>
      depsApi.report(token!, run.data!.id, reportEngine, temperature),
    onMutate: () => setReportStartedAt(Date.now()),
    onSuccess: () => {
      toast.success(t("deps.reportDone"));
      void qc.invalidateQueries({ queryKey: ["deps-latest"] });
    },
    onError: (e) => toast.error((e as Error).message),
    onSettled: () => setReportStartedAt(null),
  });

  // The audit as a file someone can send on. Not a plain <a href>: the
  // endpoint needs the bearer token, so the document is fetched and handed to
  // the browser as a blob — the same shape the documentation export uses.
  const [exporting, setExporting] = useState<"md" | "docx" | null>(null);
  const exportRun = async (format: "md" | "docx") => {
    const runId = run.data?.id;
    if (!runId) return;
    setExporting(format);
    try {
      const resp = await fetch(
        `${API_BASE}/api/deps/${runId}/export?format=${format}`,
        // requestHeaders, not a hand-rolled bearer: without the workspace hint
        // the download resolves to the account's DEFAULT workspace, so a member
        // looking at a different one exports the wrong audit or gets a 404.
        { headers: requestHeaders(token) },
      );
      if (!resp.ok) throw new Error(await resp.text());
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `celmis-dependency-audit.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setExporting(null);
    }
  };

  // The SBOM and evidence-pack buttons. Same requirement as the export above —
  // the endpoint reads the Authorization header and nothing else — but those
  // two were written as `<a href download>`, which is a browser navigation and
  // sends no such header, so both saved a 401 body instead of a file.
  const downloadExport = async (url: string, name: string) => {
    try {
      await downloadWithAuth(url, name, token);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const rows = findings.data ?? [];
  const repoOptions = useMemo(() => {
    const slugs = Array.from(new Set(rows.map((r) => r.repo_slug))).sort();
    return [{ value: "", label: t("deps.allRepos") }, ...slugs.map((s) => ({ value: s, label: s }))];
  }, [rows, t]);
  const visible = repoFilter ? rows.filter((r) => r.repo_slug === repoFilter) : rows;

  /** The lowest version that clears EVERY advisory on a package.
   *
   *  `vulns[0].fixed_in` is the first advisory's fix and it is routinely too
   *  low: axios 0.21.1 carries 24 advisories and the first one is fixed in
   *  0.31.1, while the highest needs 0.33.0. An agent told to go to 0.31.1
   *  produces a package that still has advisories against it and a PR that
   *  claims otherwise. Measured on four of six packages in one repository —
   *  axios, ws, lodash and minimist were all under-quoted.
   *
   *  Falls back to `latest_version` only when nothing states a fix, because
   *  then there is no minimal answer to prefer. */
  const minimalSafe = (f: DepFinding): string | null => {
    const fixes = (f.vulns ?? [])
      .map((v) => v.fixed_in)
      .filter((v): v is string => Boolean(v));
    if (!fixes.length) return null;
    return fixes.reduce((a, b) => (compareVersions(a, b) >= 0 ? a : b));
  };

  const fixOne = (f: DepFinding) => {
    const target = minimalSafe(f) ?? f.latest_version ?? "latest safe version";
    const prompt =
      `Update ${f.package} from ${f.current_version} to ${target} in the dependency manifests only (package.json / requirements.txt / pyproject.toml / go.mod / Cargo.toml). Do not touch unrelated dependencies. The change will be pushed to a separate branch and opened as a PR — never commit to the default branch.`;
    router.push(`/claude?${new URLSearchParams({ prompt, repo: f.repo_slug })}`);
  };

  const fixFromHere = (repoSlug: string) => {
    const targets = rows
      .filter((r) => r.repo_slug === repoSlug && r.recommendation !== "ok")
      .slice(0, 25);
    const lines = targets.map((r) => {
      // The arrow points at the TARGET, and the target is the minimal safe
      // version — not `latest_version`. It used to point at latest while the
      // footer asked for "the minimal safe upgrade", so the instruction
      // contradicted the data on every line. An agent following the arrows
      // took axios 0.21.1 -> 1.19.0, node-fetch 2.6.0 -> 3.3.2 (ESM-only),
      // express 4.16.0 -> 5.2.1 and ws 6.2.1 -> 8.21.3: four majors crossed
      // in a change the button calls safe.
      const safe = minimalSafe(r);
      const ids = r.vulns.map((v) => v.cve || v.id).filter(Boolean).join(", ");
      const target = safe ?? r.latest_version ?? "latest";
      const note = safe
        ? ` (clears all ${r.vulns.length} advisory/advisories; latest is ${r.latest_version ?? "unknown"})`
        : ` (no advisories — version drift only; latest is ${r.latest_version ?? "unknown"})`;
      return `- ${r.package}: ${r.current_version} -> ${target}${note}${ids ? ` [${ids}]` : ""}`;
    });
    const prompt =
      `Update the following dependencies and make sure the project still builds:\n\n${lines.join("\n")}\n\n` +
      `Go to the version after the arrow — it is the LOWEST version that clears every advisory on that package. ` +
      `Do NOT go to the latest version instead: a larger jump is more breaking change for no additional security. ` +
      `If a listed target is impossible (a peer conflict, an incompatible runtime), say so in the summary rather than jumping further. ` +
      `Adjust code only where an upgrade forces it, and explain each change.`;
    const q = new URLSearchParams({ prompt, repo: repoSlug });
    router.push(`/claude?${q.toString()}`);
  };

  const s = run.data?.summary;
  const extra = (s ?? {}) as DepsSummaryExtra;
  const running = run.data?.status === "running" || run.data?.status === "queued";

  // Coverage gaps are the one thing this page must never render as a silent
  // zero: an ecosystem nobody audited looks exactly like a clean one.
  const notChecked = extra.not_checked ?? [];
  const hygiene = extra.hygiene;
  const hygieneItems = hygiene?.items ?? [];
  const sourceCounts = Object.entries(extra.sources ?? {})
    .sort((a, b) => b[1] - a[1]);
  const drift = extra.drift ?? {};
  const groups = extra.groups ?? {};

  // ── Live clock, only while a run is in flight: drives the elapsed timer and
  // the stuck detection below (a plain render would freeze both).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  const startedAt = run.data ? new Date(run.data.created_at).getTime() : NaN;
  const updatedAt = run.data ? new Date(run.data.updated_at).getTime() : NaN;
  const elapsed = Number.isNaN(startedAt) ? 0 : Math.max(0, now - startedAt);
  const silentMs = Number.isNaN(updatedAt) ? 0 : Math.max(0, now - updatedAt);
  // Stuck = in flight but no progress write for STUCK_MINUTES. The run is not
  // finished and not failed, so nothing else would ever tell the user.
  const stuck = running && silentMs > STUCK_MINUTES * 60_000;
  const silentMinutes = Math.floor(silentMs / 60_000);

  const phase = s?.phase ?? "starting";
  const progress =
    typeof s?.repos_done === "number" && typeof s?.repos_total === "number"
      ? `${s.repos_done}/${s.repos_total}${s.current ? ` · ${s.current}` : ""}`
      : typeof s?.done === "number" && typeof s?.total === "number"
        ? `${s.done}/${s.total}`
        : "";
  const timing = run.data
    ? `${t("deps.startedAt", { time: formatDateTime(run.data.created_at) })} · ${
        t("deps.elapsed", { duration: formatElapsed(elapsed) })}`
    : "";
  const cancelledByUser = /cancel/i.test(run.data?.error ?? "");

  return (
    <PageShell width="wide">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold tracking-tight">
            <PackageSearchIcon className="h-6 w-6" /> {t("deps.title")}
            <span className="flex min-w-0 max-w-full items-center [&>*]:min-w-0 [&>*]:max-w-full">
              <WorkspaceBadge />
            </span>
          </h1>
          <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">{t("deps.subtitle")}</p>
          <p className="mt-1 flex max-w-2xl items-start gap-1.5 text-xs text-[var(--color-muted-foreground)]">
            <InfoIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{t("deps.engineNote")}</span>
          </p>
        </div>
        {/* Phone: the controls wrap and stretch instead of holding a fixed
            width — a non-wrapping row of min-w-* Selects is what used to make
            the whole document scroll sideways at 390px. */}
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("deps.helpTitle")} />
          <Select className="h-11 sm:h-9 min-w-0 flex-1 text-base sm:min-w-44 sm:flex-initial sm:text-xs" value={autoReport}
            onChange={(v) => setAutoReport(v as typeof autoReport)}
            options={[
              { value: "none", label: t("deps.autoReportNone") },
              { value: "api", label: t("deps.autoReportApi") },
              { value: "claude_code", label: t("deps.autoReportClaude") },
            ]} />
          {/* Scope lives in the header, always readable: which repos the next
              run covers, and how many of them. */}
          <div className="flex w-full min-w-0 items-center gap-1.5 sm:w-auto">
            <Label htmlFor="deps-scope" className="shrink-0 text-xs text-[var(--color-muted-foreground)]">
              {t("deps.scopeLabel")}
            </Label>
            <Select
              id="deps-scope"
              className="h-11 sm:h-9 min-w-0 flex-1 text-base sm:min-w-52 sm:flex-initial sm:text-xs"
              value={scopeMode === "project" ? `project:${scopeProjectId}` : scopeMode}
              onChange={(v) => {
                if (v.startsWith("project:")) {
                  setScopeMode("project");
                  setScopeProjectId(v.slice("project:".length));
                } else {
                  setScopeMode(v as ScopeMode);
                }
              }}
              options={[
                { value: "all", label: t("deps.scopeAll") },
                ...(projects.data ?? []).map((p) => ({
                  value: `project:${p.id}`,
                  label: t("deps.scopeProjectOption", { name: p.name }),
                })),
                { value: "repos", label: t("deps.scopePickRepos") },
                { value: "owner", label: t("deps.scopeByOwner") },
                { value: "developer", label: t("deps.scopeByDeveloper") },
              ]}
            />
            <Badge variant={scopeIncomplete ? "destructive" : "brand"} className="shrink-0 text-[9px]">
              {t("deps.scopeCount", { count: String(scopeCount) })}
            </Badge>
          </div>
          {/* Never a dead end: a run that stopped reporting turns this into an
              enabled "Restart", instead of an eternally disabled spinner. */}
          <Button
            className="w-full sm:w-auto"
            onClick={() => start.mutate({ force: stuck })}
            disabled={start.isPending || scopeIncomplete || (running && !stuck)}
            title={scopeIncomplete ? t("deps.scopeIncomplete") : undefined}
          >
            {running && !stuck
              ? <RefreshCwIcon className="mr-1 h-4 w-4 animate-spin" />
              : stuck
                ? <RefreshCwIcon className="mr-1 h-4 w-4" />
                : <PlayIcon className="mr-1 h-4 w-4" />}
            {stuck ? t("deps.restartAudit") : running ? t("deps.running") : t("deps.runAudit")}
          </Button>
        </div>
      </div>

      <SectionTabs set="sources" />

      {/* Above the audit, not below it. These two files are what a customer or
          a procurement department asks for by name; they used to sit at the
          very bottom of this page, under a divider, after the Word/Markdown/
          Print row — five steps from the navigation and named nowhere else in
          the product. */}
      <ComplianceArtifacts
        runId={run.data?.status === "done" ? run.data.id : undefined}
        token={token}
        repoCount={run.data?.summary?.repos_scanned}
      />

      {/* Always on screen now, not only for the two modes that need extra
          input: "which branch is this read at" is a property of every run, and
          leaving it unanswered is what makes an already-fixed vulnerability
          look like it is still there. */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">{t("deps.scopeTitle")}</CardTitle>
          <CardDescription>{t("deps.scopeDesc")}</CardDescription>
          {/* The branch answer belongs here, not in the header dialog: "why is
              a vulnerability I already fixed still listed" is always asked
              while looking at this card. */}
          <InlineHelp question={tf("deps.helpBranchTitle", "Which branch is audited")}>
            {tf("deps.helpBranchBody",
              "Each repository is cloned at the branch saved on its registration (Repositories → the branch chip); repositories with no branch saved are read at the provider's default branch. The scope card shows the resolved branch per repository and can override it for a single run.")}
          </InlineHelp>
        </CardHeader>
        <CardContent className="space-y-3">
          {scopeMode === "developer" && (
            <div>
              <Label htmlFor="deps-scope-developer">{t("deps.scopeDeveloper")}</Label>
              {(developers.data ?? []).length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {(developers.data ?? []).filter((d) => !d.is_robot).map((d) => {
                    const on = developerScope.includes(d.identity);
                    return (
                      <Button
                        key={d.identity}
                        type="button"
                        variant={on ? "default" : "outline"}
                        size="sm"
                        className="max-w-full"
                        title={
                          // The grouped identities, spelled out. A merge that
                          // cannot be inspected is a merge nobody can correct.
                          d.aliases.length
                            ? [d.identity, ...d.aliases].join("\n")
                            : d.identity
                        }
                        onClick={() =>
                          setDeveloperScope((cur) =>
                            on
                              ? cur.filter((x) => x !== d.identity)
                              : [...cur, d.identity],
                          )
                        }
                      >
                        <span className="truncate">{d.display_name || d.identity}</span>
                        <span className="ml-1.5 shrink-0 opacity-60">
                          {d.repo_count}
                        </span>
                        {d.aliases.length > 0 && (
                          <span className="ml-1 shrink-0 opacity-60">
                            {t("deps.scopeDeveloperAliases", { count: d.aliases.length + 1 })}
                          </span>
                        )}
                      </Button>
                    );
                  })}
                </div>
              ) : (
                // Ownership is computed, not fetched — an empty list means the
                // snapshot has never been built, which is a thing to go and do
                // rather than an error to report.
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {developers.isLoading
                    ? t("deps.scopeDeveloperLoading")
                    : t("deps.scopeDeveloperEmpty")}
                </p>
              )}
              {developerScope.length > 0 && (
                <p className="mt-1 text-xs text-[var(--color-brand)]">
                  {t("deps.scopeDeveloperPicked", {
                    repos: developerRepos.length,
                    people: developerScope.length,
                  })}
                </p>
              )}
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                {t("deps.scopeDeveloperHint")}
              </p>
            </div>
          )}
          {scopeMode === "owner" && (
            <div>
              <Label htmlFor="deps-scope-owner">{t("deps.scopeOwner")}</Label>
              {ownerOptions.length > 0 ? (
                <Select
                  id="deps-scope-owner"
                  value={ownerScope}
                  onChange={setOwnerScope}
                  options={ownerOptions}
                  className="w-full"
                  placeholder={t("deps.scopeOwnerPlaceholder")}
                />
              ) : (
                // No registered repo carries an "owner/name" — nothing to
                // offer, so the field stays free text rather than an empty menu.
                <Input id="deps-scope-owner" placeholder="owner / bitbucket-username"
                  value={ownerScope} onChange={(e) => setOwnerScope(e.target.value)} />
              )}
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                {t("deps.scopeOwnerHint")}
              </p>
            </div>
          )}
          {scopeMode === "repos" && (
            <div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label>{t("deps.scopeRepos")}</Label>
                <div className="flex flex-wrap gap-1">
                  <Button size="sm" variant="ghost" className="h-11 sm:h-8"
                    onClick={() => setRepoScope(new Set(allRepos.map((r) => r.slug)))}>
                    {t("deps.scopeSelectAll")}
                  </Button>
                  <Button size="sm" variant="ghost" className="h-11 sm:h-8" disabled={repoScope.size === 0}
                    onClick={() => setRepoScope(new Set())}>
                    {t("deps.scopeClear")}
                  </Button>
                </div>
              </div>
              <div className="mt-1 grid gap-1 sm:grid-cols-2">
                {allRepos.map((r) => (
                  <label key={r.slug} className="flex min-h-11 min-w-0 items-center gap-2 text-sm sm:min-h-0">
                    <input type="checkbox" className="shrink-0" checked={repoScope.has(r.slug)}
                      onChange={(e) => {
                        const next = new Set(repoScope);
                        if (e.target.checked) next.add(r.slug); else next.delete(r.slug);
                        setRepoScope(next);
                      }} />
                    <span className="min-w-0 flex-1 truncate">{r.full_name}</span>
                    {/* The ref this repo's manifests are read from, on the
                        same line as the tick that includes it. */}
                    <code className={`shrink-0 text-[10px] ${branchForRun
                      ? "text-[var(--color-brand)]"
                      : "text-[var(--color-muted-foreground)]"}`}>
                      {branchForRun || r.branch
                        || tf("deps.branchProviderDefault", "provider default")}
                    </code>
                  </label>
                ))}
              </div>
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                {t("deps.scopeReposHint")}
              </p>
            </div>
          )}

          {/* ── Which branch the audit reads ──────────────────────────
              Until this was written down, "why is this vulnerability still
              listed" had no visible answer: the audit clones each repo at
              the branch stored on its registration, which is a setting
              three pages away from here. */}
          <div className="space-y-2 rounded-md border border-[var(--color-border)] p-3">
            <div className="flex flex-wrap items-center gap-2">
              <GitBranchIcon className="h-4 w-4 shrink-0" />
              <span className="text-xs font-medium">
                {tf("deps.branchTitle", "Branch")}
              </span>
              {branchForRun && (
                <Badge variant="brand" className="text-[9px] wrap-anywhere">
                  {tf("deps.branchOverrideActive", "override: {branch}",
                    { branch: branchForRun })}
                </Badge>
              )}
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {tf("deps.branchNote",
                "With no override, every repository is audited at the branch saved on its registration (Repositories → the branch chip on each row). Repositories with no branch saved are read at the provider's default branch.")}
            </p>
            <div>
              <Label htmlFor="deps-branch-override">
                {tf("deps.branchOverrideLabel", "Audit this branch instead (optional)")}
              </Label>
              {/* One control, not two. A dropdown and a text box bound to the
                  same value showed the branch twice and read as a duplicate —
                  which it was. The list comes from the repositories in scope,
                  so typing is only needed for a branch none of them has. */}
              {branchOptions.length > 0 && !typeBranch ? (
                <Select
                  id="deps-branch-override"
                  value={branchOptions.some((o) => o.value === branchForRun) ? branchForRun : ""}
                  onChange={setBranchOverride}
                  options={[
                    { value: "", label: tf("deps.branchKeepEach", "Keep each repository's own branch") },
                    ...branchOptions,
                  ]}
                  className="w-full"
                  placeholder={tf("deps.branchPickPlaceholder", "Pick a branch…")}
                />
              ) : (
                <Input id="deps-branch-override" value={branchOverride}
                  onChange={(e) => setBranchOverride(e.target.value)}
                  placeholder={tf("deps.branchOverridePlaceholder", "e.g. develop")}
                  autoComplete="off" autoCapitalize="none" spellCheck={false} />
              )}
              {branchOptions.length > 0 && (
                <button
                  type="button"
                  className="mt-1 text-xs underline opacity-70 hover:opacity-100"
                  onClick={() => setTypeBranch((v) => !v)}
                >
                  {tf(typeBranch ? "deps.branchPickInstead" : "deps.branchTypeInstead",
                    typeBranch ? "Pick from the list" : "Type a branch name")}
                </button>
              )}
              {branchesLoading && (
                <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                  {tf("deps.branchLoading", "Reading branches from the provider…")}
                </p>
              )}
              {/* A branch nobody has is the quiet failure this warns about:
                  the run falls back to each repo's own ref and reads something
                  other than what the field says. */}
              {branchForRun && branchOptions.length > 0
                && !branchOptions.some((o) => o.value === branchForRun) && (
                <p className="mt-1 text-xs text-[var(--color-destructive)]">
                  {tf("deps.branchUnknown",
                    "No repository in this scope has a branch named “{branch}”.",
                    { branch: branchForRun })}
                </p>
              )}
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                {tf("deps.branchOverrideHint",
                  "Applies to every repository in this run and to this run only — it is not saved on the repositories. Leave it empty to keep each repository's own branch.")}
              </p>
            </div>
            {/* Per-repo resolution. In "pick repos" mode the checkbox list
                above already carries it, so repeating it here would just be
                a second list to keep in sync. */}
            {scopeMode !== "repos" && scopedRepos.length > 0 && (
              <div>
                <div className="text-xs font-medium">
                  {tf("deps.branchPerRepoTitle", "This run will read")}
                </div>
                {/* Was a read-only list. It named the branch each repo would
                    be read at and gave no way to change it — the setting lived
                    three pages away, on Repositories, one repo at a time. */}
                <div className="mt-1">
                  <RepoBranchTable repos={scopedRepos} overrideBranch={branchForRun} />
                </div>
              </div>
            )}
          </div>

          {scopeIncomplete && (
            <p className="text-xs text-[var(--color-destructive)]">
              {t("deps.scopeIncomplete")}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Only once the project list has actually loaded — otherwise the first
          paint accuses a perfectly fine project of being empty. */}
      {scopeMode === "project" && scopeIncomplete && !projects.isPending && (
        <Callout tone="warning">{t("deps.scopeProjectEmpty")}</Callout>
      )}

      {running && (
        <Card>
          <CardContent className="flex items-start gap-3 py-4 text-sm">
            <RefreshCwIcon
              className={`mt-0.5 h-4 w-4 text-[var(--color-brand)] ${stuck ? "" : "animate-spin"}`}
            />
            <div className="min-w-0">
              <div className="font-medium">{t(`deps.phase.${phase}`)}</div>
              {progress && (
                <div className="text-xs text-[var(--color-muted-foreground)]">{progress}</div>
              )}
              <div className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                {timing}
                {" · "}
                {t("deps.lastProgress", { time: formatDateTime(run.data!.updated_at) })}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {stuck && (
        <Callout tone="warning">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="font-medium">
                {t("deps.stuckTitle", { minutes: String(silentMinutes) })}
              </p>
              <p className="mt-0.5 opacity-90">{t("deps.stuckBody")}</p>
              <p className="mt-0.5 opacity-80">{timing}</p>
            </div>
            <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
              <Button size="sm" className="h-11 sm:h-8" disabled={start.isPending}
                onClick={() => start.mutate({ force: true })}>
                <RefreshCwIcon className={`mr-1 h-3.5 w-3.5 ${start.isPending ? "animate-spin" : ""}`} />
                {t("deps.restartAudit")}
              </Button>
              <Button size="sm" variant="outline" className="h-11 sm:h-8" disabled={cancel.isPending}
                onClick={() => cancel.mutate()}>
                <CircleSlashIcon className="mr-1 h-3.5 w-3.5" />
                {t("deps.cancelAudit")}
              </Button>
            </div>
          </div>
        </Callout>
      )}

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("deps.helpTitle")}</DialogTitle>
            <DialogDescription>{t("deps.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <Callout tone="info">{t("deps.engineNote")}</Callout>
            <ol className="list-decimal space-y-1.5 pl-5 text-xs text-[var(--color-muted-foreground)]">
              <li>{t("deps.helpS1")}</li>
              <li>{t("deps.helpS2")}</li>
              <li>{t("deps.helpS3")}</li>
              <li>{t("deps.helpS4")}</li>
              <li>{t("deps.helpS5")}</li>
            </ol>
            <div>
              <div className="mb-1 font-medium">{t("deps.helpEngineTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("deps.helpEngineBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("deps.helpFixTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("deps.helpFixBody")}</p>
            </div>
            {/* Same sentence as the scope card — this dialog is where people
                look when the findings disagree with the code they just fixed. */}
            <div>
              <div className="mb-1 font-medium">
                {tf("deps.helpBranchTitle", "Which branch is audited")}
              </div>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {tf("deps.helpBranchBody",
                  "Each repository is cloned at the branch saved on its registration (Repositories → the branch chip); repositories with no branch saved are read at the provider's default branch. The scope card shows the resolved branch per repository and can override it for a single run.")}
              </p>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {run.data?.status === "error" && (
        <Callout tone={cancelledByUser ? "warning" : "danger"}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="font-medium">
                {cancelledByUser ? t("deps.cancelledTitle") : t("deps.errorTitle")}
              </p>
              <p className="mt-0.5 whitespace-pre-wrap break-words opacity-90">
                {run.data.error || t("deps.errorUnknown")}
              </p>
              <p className="mt-0.5 opacity-80">
                {t("deps.startedAt", { time: formatDateTime(run.data.created_at) })}
                {" · "}
                {t("deps.lastProgress", { time: formatDateTime(run.data.updated_at) })}
              </p>
            </div>
            <Button size="sm" variant="outline" className="h-11 sm:h-8" disabled={start.isPending}
              onClick={() => start.mutate({ force: true })}>
              <RefreshCwIcon className={`mr-1 h-3.5 w-3.5 ${start.isPending ? "animate-spin" : ""}`} />
              {t("deps.restartAudit")}
            </Button>
          </div>
        </Callout>
      )}

      {s && run.data?.status === "done" && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat
            label={t("deps.statRepos")}
            value={<><CountUp value={s.repos_scanned ?? 0} />/{s.repos_total ?? 0}</>}
          />
          <Stat label={t("deps.statPackages")} value={<CountUp value={s.packages ?? 0} />} />
          <Stat label={t("deps.statOutdated")} value={<CountUp value={s.outdated ?? 0} />} />
          <Stat
            label={t("deps.statVulnerable")}
            value={<CountUp value={s.vulnerable ?? 0} />}
            accent={(s.vulnerable ?? 0) > 0}
          />
          {/* The whole point of the native auditors: packages nobody declared,
              pulled in by something that was. Previously invisible. */}
          <Stat
            label={t("deps.statTransitive")}
            value={
              <>
                <CountUp value={extra.transitive_vulnerable ?? 0} />
                <span className="text-base font-normal opacity-60">
                  /{extra.transitive ?? 0}
                </span>
              </>
            }
            accent={(extra.transitive_vulnerable ?? 0) > 0}
          />
        </div>
      )}
      {run.data?.status === "done" && (s?.repos_total ?? 0) === 0 && (
        <Callout tone="info">{t("deps.noReposInScope")}</Callout>
      )}
      {(s?.vuln_check_errors ?? 0) > 0 && run.data?.status === "done" && (
        <Callout tone="warning">{t("deps.vulnCheckFailed")}</Callout>
      )}
      {!!s?.repos_skipped?.length && (
        <Callout tone="warning">
          <p className="font-medium">{t("deps.skippedTitle", { n: String(s.repos_skipped.length) })}</p>
          <ul className="mt-1 space-y-0.5 wrap-anywhere">
            {s.repos_skipped.map((slug) => (
              <li key={slug}>
                <span className="font-mono">{slug}</span>
                {s.skip_reasons?.[slug] ? ` — ${s.skip_reasons[slug]}` : ""}
              </li>
            ))}
          </ul>
          <p className="mt-1 opacity-80">{t("deps.skippedHint")}</p>
        </Callout>
      )}

      {/* ── Coverage: which tool answered, and who did NOT ────────────
          An ecosystem with no auditor and no lock file produces zero findings
          that look identical to a clean bill of health. Saying so out loud is
          the difference between a report and a false sense of safety. */}
      {run.data?.status === "done" && (notChecked.length > 0 || sourceCounts.length > 0) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <EyeOffIcon className="h-4 w-4" /> {t("deps.coverageTitle")}
            </CardTitle>
            <CardDescription>{t("deps.coverageDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {sourceCounts.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {t("deps.sourcesLabel")}
                </span>
                {sourceCounts.map(([name, count]) => (
                  <Badge key={name} variant={name === "osv" ? "default" : "brand"}
                    className="text-[10px]">
                    {name} · {count}
                  </Badge>
                ))}
              </div>
            )}
            {notChecked.length === 0 ? (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("deps.coverageAllChecked")}
              </p>
            ) : (
              <div className="space-y-1.5">
                <p className="text-xs font-medium text-amber-500">
                  {t("deps.notCheckedTitle", { n: String(notChecked.length) })}
                </p>
                <ul className="space-y-1 text-xs">
                  {notChecked.slice(0, 30).map((c, i) => (
                    <li key={`${c.repo}-${c.ecosystem}-${c.subproject}-${i}`}
                      className="flex flex-wrap items-baseline gap-1.5 wrap-anywhere">
                      <Badge variant="outline" className="text-[9px]">{c.ecosystem}</Badge>
                      {/* "partial" is in this list because a scan that saw the
                          direct dependencies and not the tree is genuinely
                          incomplete — but calling it "not checked" would
                          understate it in the other direction. */}
                      {c.status === "partial" && (
                        <Badge variant="brand" className="text-[9px]">
                          {tf("deps.checkPartial", "partial")}
                        </Badge>
                      )}
                      <code className="opacity-80">{c.tool}</code>
                      {c.repo && (
                        <span className="text-[var(--color-muted-foreground)]">
                          {c.repo}{c.subproject ? `/${c.subproject}` : ""}
                        </span>
                      )}
                      <span className="text-[var(--color-muted-foreground)]">
                        — {c.reason || t("deps.notCheckedNoReason")}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("deps.notCheckedHint")}
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Version drift, deliberately NOT mixed with vulnerabilities ──
          "Two minors behind" and "has a CVE" are different problems with
          different urgency; one table column used to blur them together. */}
      {run.data?.status === "done" && Object.keys(drift).length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <GaugeIcon className="h-4 w-4" /> {t("deps.driftTitle")}
            </CardTitle>
            <CardDescription>{t("deps.driftDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <DriftBucket label={t("deps.groupUpdateNow")} value={groups.update_now ?? 0}
              hint={t("deps.groupUpdateNowHint")} accent />
            <DriftBucket label={t("deps.groupPlanMajor")} value={drift.major ?? 0}
              hint={t("deps.groupPlanMajorHint")} />
            <DriftBucket label={t("deps.groupUpdateSafe")}
              value={(drift.minor ?? 0) + (drift.patch ?? 0)}
              hint={t("deps.groupUpdateSafeHint")} />
            {/* "Current" is a number and nothing else — a list of healthy
                packages is the one thing nobody reads. */}
            <DriftBucket label={t("deps.groupCurrent")} value={drift.none ?? 0}
              hint={(drift.unknown ?? 0) > 0
                ? t("deps.groupCurrentUnknown", { n: String(drift.unknown ?? 0) })
                : t("deps.groupCurrentHint")} />
          </CardContent>
        </Card>
      )}

      {/* ── Supply-chain hygiene — separate from CVEs on purpose ────── */}
      {run.data?.status === "done" && (hygiene?.total ?? 0) > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <WrenchIcon className="h-4 w-4" /> {t("deps.hygieneTitle")}
              <Badge variant="brand" className="text-[9px]">{hygiene?.total ?? 0}</Badge>
            </CardTitle>
            <CardDescription>{t("deps.hygieneDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {HYGIENE_KINDS.filter((k) => (hygiene?.by_kind?.[k] ?? 0) > 0).map((k) => (
                <Badge key={k} variant="outline" className="text-[10px]">
                  {t(`deps.hygieneKind.${k}`)} · {hygiene?.by_kind?.[k] ?? 0}
                </Badge>
              ))}
            </div>
            <div className="space-y-1.5">
              {hygieneItems.slice(0, 60).map((item, i) => (
                <div key={`${item.repo}-${item.package}-${i}`}
                  className="rounded-md border border-[var(--color-border)] p-2 text-xs wrap-anywhere">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={HYGIENE_TONE[item.severity] ?? "default"}
                      className="text-[9px]">
                      {t(`deps.hygieneKind.${item.kind}`)}
                    </Badge>
                    <code className="font-medium">{item.package}</code>
                    <span className="text-[var(--color-muted-foreground)]">
                      {item.ecosystem}
                    </span>
                    {item.repo && (
                      <span className="text-[var(--color-muted-foreground)]">
                        · {item.repo}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 opacity-90">{item.detail}</p>
                  <p className="mt-0.5 font-mono text-[10px] opacity-60">
                    {item.location}{item.line ? `:${item.line}` : ""}
                  </p>
                  {/* The line itself. Without it this row is a claim about a
                      file, and a deterministic check whose evidence cannot be
                      looked at reads exactly like a guess. */}
                  {item.excerpt && (
                    <code className="mt-1 block overflow-x-auto whitespace-pre rounded bg-[var(--color-muted)]/40 p-1.5 text-[10px]">
                      {item.excerpt}
                    </code>
                  )}
                </div>
              ))}
            </div>
            {(hygiene?.total ?? 0) > hygieneItems.length && (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("deps.hygieneTruncated", {
                  shown: String(hygieneItems.length),
                  total: String(hygiene?.total ?? 0),
                })}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {run.data?.status === "done" && (
        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle className="flex items-center gap-2 text-base">
                  <FileTextIcon className="h-4 w-4" /> {t("deps.reportTitle")}
                </CardTitle>
                <CardDescription>{t("deps.reportDesc")}</CardDescription>
                {/* Beside the picker itself: "does a model decide which CVEs
                    I have" is the doubt that makes people distrust the whole
                    page, and the answer is no — it writes prose over facts
                    already collected. */}
                <InlineHelp
                  className="mt-1"
                  question={tf("deps.helpEngineTitle", "Where the engine choice applies")}
                >
                  {tf("deps.helpEngineBody",
                    "The audit itself never calls an LLM. The engine picker chooses who writes the executive report over the facts.")}
                </InlineHelp>
              </div>
              <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
                <Select className="h-11 sm:h-8 min-w-0 flex-1 text-base sm:min-w-40 sm:flex-initial sm:text-xs" value={reportEngine}
                  onChange={(v) => setReportEngine(v as "api" | "claude_code")}
                  options={[
                    { value: "api", label: t("deps.reportEngineApi") },
                    { value: "claude_code", label: t("deps.reportEngineClaude") },
                  ]} />
                <Button size="sm" className="h-11 sm:h-8" disabled={report.isPending} onClick={() => report.mutate()}>
                  {report.isPending
                    ? <RefreshCwIcon className="mr-1 h-3.5 w-3.5 animate-spin" />
                    : <FileTextIcon className="mr-1 h-3.5 w-3.5" />}
                  {t("deps.reportButton")}
                </Button>
              </div>
            </div>
            {/* A spinner with no elapsed time and no expectation reads as
                "broken" after ninety seconds. It is not: the run takes minutes
                and the result is written to the run before the reply. */}
            {report.isPending && (
              <div className="mt-2 flex items-start gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/20 px-3 py-2 text-xs">
                <RefreshCwIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-brand)]" />
                <span>
                  {tf("deps.reportRunning",
                      "Writing the report — {elapsed}s elapsed.",
                      { elapsed: reportElapsed })}
                  {" "}
                  {reportEngine === "claude_code"
                    ? tf("deps.reportRunningClaude",
                         "Claude Code starts a fresh session for this; one to "
                         + "three minutes is normal.")
                    : tf("deps.reportRunningApi",
                         "This usually takes under a minute.")}
                  {" "}
                  {tf("deps.reportRunningSafe",
                      "It is saved to the audit when it finishes — you can "
                      + "leave this page and come back.")}
                </span>
              </div>
            )}
            {/* The bare "0.2" box used to sit unlabelled next to the engine
                picker — nobody could tell what it controlled. */}
            {reportEngine === "api" && (
              <div className="mt-2 flex items-center gap-2">
                <Tooltip label={t("deps.temperatureHint")} side="right">
                  <Label htmlFor="deps-report-temp" tabIndex={0}
                    className="cursor-help text-xs text-[var(--color-muted-foreground)] underline decoration-dotted underline-offset-4">
                    {t("deps.temperature")}
                  </Label>
                </Tooltip>
                <Input id="deps-report-temp" className="h-11 sm:h-8 w-16 text-base sm:text-xs" type="number"
                  min={0} max={2} step={0.1}
                  value={reportTemp} onChange={(e) => setReportTemp(e.target.value)}
                  title={t("deps.reportTempTitle")} />
              </div>
            )}
          </CardHeader>
          {run.data.summary.ai_report && (
            <CardContent>
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/20 p-4">
                {/* Attribution inside the box, not under it: this text gets
                    copied into tickets and pasted into mail, and a report that
                    arrives without a source reads as somebody's opinion. */}
                <p className="mb-3 border-b border-[var(--color-border)] pb-2 text-xs font-medium tracking-wide text-[var(--color-brand)]">
                  {tf("deps.reportStamp", "Celmis — Dependency Audit")}
                </p>
                <div className="whitespace-pre-wrap wrap-anywhere text-sm leading-relaxed">
                  {run.data.summary.ai_report}
                </div>
              </div>
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                {t("deps.reportBy", { engine: run.data.summary.ai_report_engine ?? "api" })}
              </p>
            </CardContent>
          )}
          {run.data.summary.ai_report_error && (
            <CardContent>
              <p className="wrap-anywhere text-xs text-red-500">
                {t("deps.reportFailed", { error: run.data.summary.ai_report_error })}
              </p>
            </CardContent>
          )}
        </Card>
      )}

      {/* ── Export ────────────────────────────────────────────────────
          Everything above this point lives in four collapsible panels in a
          browser session. A person handing the result to a security reviewer,
          a client or a ticket needs to take it with them. */}
      {run.data?.status === "done" && (
        <Card className="print:hidden">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <DownloadIcon className="h-4 w-4" />
              {tf("deps.exportTitle", "Export the audit")}
            </CardTitle>
            <CardDescription>
              {tf("deps.exportDesc",
                  "One document: overview, what nobody checked, the report, and "
                  + "every finding grouped by repository.")}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" className="h-11 sm:h-8"
              disabled={exporting !== null}
              onClick={() => void exportRun("docx")}>
              {exporting === "docx"
                ? <RefreshCwIcon className="mr-1 h-3.5 w-3.5 animate-spin" />
                : <FileTextIcon className="mr-1 h-3.5 w-3.5" />}
              {tf("deps.exportDocx", "Word (.docx)")}
            </Button>
            <Button size="sm" variant="outline" className="h-11 sm:h-8"
              disabled={exporting !== null}
              onClick={() => void exportRun("md")}>
              {exporting === "md"
                ? <RefreshCwIcon className="mr-1 h-3.5 w-3.5 animate-spin" />
                : <DownloadIcon className="mr-1 h-3.5 w-3.5" />}
              {tf("deps.exportMd", "Markdown (.md)")}
            </Button>
            <Button size="sm" variant="outline" className="h-11 sm:h-8"
              onClick={() => window.print()}>
              <PrinterIcon className="mr-1 h-3.5 w-3.5" />
              {tf("deps.exportPdf", "Print / PDF")}
            </Button>
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {tf("deps.exportPdfHint",
                  "PDF comes from your browser's print dialog — choose "
                  + "“Save as PDF”.")}
            </span>
          </CardContent>

          {/* A second row, and a second audience.
              Everything above is a document somebody READS — three formats of
              the same prose. These two are artefacts somebody FILES, and they
              had no button at all: the SBOM could not be obtained by any
              means, and the evidence pack only by hand-writing a curl. The
              gap was systematic — everything addressed to a developer was
              reachable, everything addressed to a buyer was not. */}
          <CardContent className="flex flex-wrap items-center gap-2 border-t border-[var(--color-border)] pt-4">
            {/* Buttons, not `<a href download>`. A browser navigation sends
                cookies and no Authorization header, and these endpoints read
                that header and nothing else — so both of these saved a file
                containing {"detail":"Missing or invalid Authorization header"}
                under a button labelled "Download SBOM". The /export button a
                few lines up always did it correctly, which is exactly why
                nobody looked at these two. */}
            <Button size="sm" variant="outline" className="h-11 sm:h-8"
                    disabled={!run.data?.id}
                    onClick={() => run.data?.id && downloadExport(
                      depsApi.sbomUrl(run.data.id),
                      `celmis-sbom-${run.data.id}.cdx.json`)}>
              <PackageSearchIcon className="mr-1 h-3.5 w-3.5" />
              {t("deps.downloadSbom")}
            </Button>
            <Button size="sm" variant="outline" className="h-11 sm:h-8"
                    disabled={!run.data?.id}
                    onClick={() => run.data?.id && downloadExport(
                      depsApi.evidenceUrl(run.data.id),
                      `celmis-evidence-${run.data.id}.zip`)}>
              <ShieldCheckIcon className="mr-1 h-3.5 w-3.5" />
              {t("deps.downloadEvidence")}
            </Button>
            <InlineHelp className="w-full" question={t("deps.whichExport")}>
              {t("deps.whichExportBody")}
            </InlineHelp>
          </CardContent>
        </Card>
      )}

      {run.data?.status === "done" && (
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>{t("deps.findingsTitle")}</CardTitle>
                <CardDescription>{t("deps.findingsDesc")}</CardDescription>
              </div>
              <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
                <Select value={filter} className="h-11 sm:h-8 min-w-0 flex-1 text-base sm:flex-initial sm:text-xs"
                  onChange={(v) => setFilter(v as typeof filter)}
                  options={[
                    { value: "vulnerable", label: t("deps.filterVulnerable") },
                    { value: "outdated", label: t("deps.filterOutdated") },
                    { value: "all", label: t("deps.filterAll") },
                  ]} />
                <Select value={repoFilter} className="h-11 sm:h-8 min-w-0 flex-1 text-base sm:min-w-40 sm:flex-initial sm:text-xs"
                  onChange={setRepoFilter} options={repoOptions} />
                {repoFilter ? (
                  <Button size="sm" className="h-11 w-full sm:h-8 sm:w-auto" onClick={() => fixFromHere(repoFilter)}>
                    <BotIcon className="mr-1 h-3.5 w-3.5" /> {t("deps.fixFromHere")}
                  </Button>
                ) : (
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    {t("deps.fixHint")}
                  </span>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {findings.isLoading && (
              <div className="py-6 text-center text-sm text-[var(--color-muted-foreground)]">
                {t("common.loading")}
              </div>
            )}
            {!findings.isLoading && visible.length === 0 && (
              repoFilter ? (
                <EmptyState icon={ShieldCheckIcon} title={t("deps.noFindingsRepo")} />
              ) : filter === "vulnerable" ? (
                <EmptyState
                  icon={ShieldCheckIcon}
                  title={t("deps.noVulns")}
                  description={t("deps.noVulnsDesc", { packages: String(s?.packages ?? 0) })}
                />
              ) : filter === "outdated" ? (
                <EmptyState
                  icon={ShieldCheckIcon}
                  title={t("deps.noOutdated")}
                  description={t("deps.noOutdatedDesc", { packages: String(s?.packages ?? 0) })}
                />
              ) : (
                // filter "all" with zero rows can only mean nothing was found
                // to audit — no manifests (or no repos) in scope.
                <EmptyState
                  icon={PackageSearchIcon}
                  title={t("deps.noPackages")}
                  description={t("deps.noPackagesDesc")}
                />
              )
            )}
            <div className="space-y-2">
              {visible.map((f) => <FindingRow key={f.id} f={f} fixOne={fixOne} />)}
            </div>
          </CardContent>
        </Card>
      )}

      {!run.data && !run.isLoading && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-[var(--color-muted-foreground)]">
            {t("deps.empty")}
          </CardContent>
        </Card>
      )}
    </PageShell>
  );
}

function Stat({ label, value, accent }: { label: string; value: ReactNode; accent?: boolean }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] p-3">
      <div className="text-xs text-[var(--color-muted-foreground)]">{label}</div>
      <div className={`text-2xl font-semibold ${accent ? "text-red-500" : ""}`}>{value}</div>
    </div>
  );
}

function DriftBucket({ label, value, hint, accent }: {
  label: string; value: number; hint: string; accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] p-3">
      <div className={`text-2xl font-semibold ${accent && value > 0 ? "text-red-500" : ""}`}>
        <CountUp value={value} />
      </div>
      <div className="text-xs font-medium">{label}</div>
      <div className="mt-0.5 text-[11px] leading-snug text-[var(--color-muted-foreground)]">
        {hint}
      </div>
    </div>
  );
}

function FindingRow({ f, fixOne }: { f: DepFinding; fixOne: (f: DepFinding) => void }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const recLabel: Record<DepFinding["recommendation"], string> = {
    update_now: t("deps.recUpdateNow"),
    update_safe: t("deps.recUpdateSafe"),
    plan_major: t("deps.recPlanMajor"),
    ok: t("deps.recOk"),
  };
  // A package nobody declared, reported only because a native auditor walked
  // the resolved tree — worth flagging, because "update it" means updating
  // whatever pulled it in, not this line in a manifest.
  const transitive = f.vulns.some((v) => vulnMeta(v).transitive);
  const sources = findingSources(f);
  const subproject = f.vulns.map((v) => vulnMeta(v).subproject).find(Boolean);
  return (
    <div className="rounded-md border border-[var(--color-border)] p-3 text-sm wrap-anywhere">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {f.severity !== "none" && (
            <Badge variant={SEV_VARIANT[f.severity] ?? "default"}>
              <ShieldAlertIcon className="mr-1 h-3 w-3" /> {f.severity}
            </Badge>
          )}
          <code className="font-medium">{f.package}</code>
          <span className="text-xs text-[var(--color-muted-foreground)]">{f.ecosystem}</span>
          {f.is_dev && <Badge className="text-[9px]">dev</Badge>}
          {/* `title` rather than <Tooltip>: the bubble is whitespace-nowrap,
              and this explanation is a sentence, not a label. */}
          {transitive && (
            <Badge variant="outline" className="cursor-help text-[9px]"
              title={t("deps.transitiveHint")}>
              <LayersIcon className="mr-1 h-3 w-3" /> {t("deps.transitive")}
            </Badge>
          )}
          {sources.map((src) => (
            <Badge key={src} variant={src === "osv" ? "default" : "brand"}
              className="text-[9px]">
              {src}
            </Badge>
          ))}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
          <code>{f.current_version}</code>
          <span className="opacity-50">→</span>
          <code className={f.outdated === "none" ? "" : "text-[var(--color-brand)]"}>
            {f.latest_version ?? "?"}
          </code>
          {f.outdated !== "none" && <Badge variant="outline">{f.outdated}</Badge>}
          <Badge variant={f.recommendation === "update_now" ? "destructive" : "default"}>
            {recLabel[f.recommendation]}
          </Badge>
          {f.recommendation !== "ok" && (
            <Button size="sm" variant="outline"
              className="h-11 px-3 text-xs sm:h-8 sm:px-2 sm:text-[10px]" onClick={() => fixOne(f)}>
              <BotIcon className="mr-1 h-3 w-3" /> {t("deps.fixOne")}
            </Button>
          )}
        </div>
      </div>
      <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">
        {f.repo_slug}{subproject ? ` · ${subproject}` : ""}
      </div>
      {/* Negative margins buy a 44px tap target without moving the link:
          -2.5 = the old mt-1 minus half the added height, -3.5 = the other half. */}
      {f.vulns.length > 0 && (
        <button type="button" onClick={() => setOpen((v) => !v)}
          className="-mt-2.5 -mb-3.5 flex w-fit min-h-11 items-center text-xs text-[var(--color-brand)] underline">
          {open ? t("deps.hideVulns") : t("deps.showVulns", { count: f.vulns.length })}
        </button>
      )}
      {open && (
        <div className="mt-2 space-y-1.5">
          {f.vulns.map((v) => (
            <div key={v.id} className="rounded border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-2 py-1.5 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                {/* Not every source ships an advisory URL (npm's chain
                    entries, abandoned-package notices) — an empty href would
                    silently reload this page. */}
                {v.url ? (
                  <a href={v.url} target="_blank" rel="noreferrer" className="font-mono underline">
                    {v.cve || v.id}
                  </a>
                ) : (
                  <span className="font-mono">{v.cve || v.id}</span>
                )}
                <Badge variant={SEV_VARIANT[v.severity] ?? "default"} className="text-[9px]">{v.severity}</Badge>
                <Badge variant="outline" className="text-[9px]">
                  {vulnMeta(v).source || "osv"}
                </Badge>
                {v.fixed_in
                  ? <span className="text-emerald-500">{t("deps.fixedIn", { version: v.fixed_in })}</span>
                  : <span className="text-amber-500">{t("deps.noFixYet")}</span>}
              </div>
              {v.summary && <p className="mt-0.5 opacity-80">{v.summary}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
