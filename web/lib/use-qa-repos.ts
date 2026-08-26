"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, qaApi, type AvailableRepo, type RepoOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";

/**
 * A repo as offered to the Q&A project UI.
 *
 * Merges the two backends that each know only half the truth:
 *   - GET /api/repos          → every repo registered in the workspace
 *   - GET /api/qa/available-repos → only repos that already have vault points
 *     (it is built by scanning Qdrant, so a freshly added repo is absent).
 *
 * Using available-repos alone made just-added repos impossible to pick.
 */
export interface QaRepoOption {
  slug: string;
  /** Vault points in Qdrant — 0 when no vault has been generated yet. */
  vaultPoints: number;
  /** True when the repo can actually be searched semantically. */
  hasVault: boolean;
  /** Projects already containing this repo (only known for vault-ready ones). */
  inProjects: string[];
}

function merge(repos: RepoOut[], available: AvailableRepo[]): QaRepoOption[] {
  const vaultBySlug = new Map(available.map((r) => [r.repo_slug, r]));
  const out: QaRepoOption[] = repos.map((r) => {
    const v = vaultBySlug.get(r.slug);
    return {
      slug: r.slug,
      vaultPoints: v?.vault_points ?? 0,
      hasVault: (v?.vault_points ?? 0) > 0,
      inProjects: v?.in_projects ?? [],
    };
  });

  // Vault points can outlive the repo registration (repo removed, vault kept),
  // and they may be visible to the caller through fine-grained access. Keep
  // those rows so nothing that used to be selectable silently disappears.
  const known = new Set(out.map((r) => r.slug));
  for (const v of available) {
    if (known.has(v.repo_slug)) continue;
    out.push({
      slug: v.repo_slug,
      vaultPoints: v.vault_points,
      hasVault: v.vault_points > 0,
      inProjects: v.in_projects,
    });
  }

  // Vault-ready first (most points first), then the rest alphabetically.
  return out.sort((a, b) => {
    if (a.hasVault !== b.hasVault) return a.hasVault ? -1 : 1;
    if (a.vaultPoints !== b.vaultPoints) return b.vaultPoints - a.vaultPoints;
    return a.slug.localeCompare(b.slug);
  });
}

/** Every workspace repo, annotated with its vault readiness. */
export function useQaRepos() {
  const token = useToken();

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token: token! }),
    enabled: !!token,
  });
  const available = useQuery({
    queryKey: ["qa", "available-repos"],
    queryFn: () => qaApi.availableRepos(token!),
    enabled: !!token,
  });

  const options = useMemo(
    () => merge(repos.data ?? [], available.data ?? []),
    [repos.data, available.data],
  );

  return {
    options,
    /** Settled once the workspace repo list is known — vault info may lag. */
    isLoading: repos.isLoading,
    error: repos.error ?? available.error,
  };
}
