import type { RepoOut } from "@/lib/api";
import { filterOptions } from "@/lib/pickers";

type Searchable = Pick<RepoOut, "slug" | "full_name" | "provider" | "display_name">;

/** The repositories matching every word of `query` in their name (owner
 *  included), slug, upload name or provider, in the list's own order. Built on
 *  the pickers' matcher so typing behaves the same everywhere: words in any
 *  order, case and accents ignored, an empty query keeps everything. */
export function filterRepos<T extends Searchable>(repos: readonly T[], query: string): T[] {
  return filterOptions(
    repos.map((repo) => ({
      value: repo.slug,
      label: repo.full_name,
      keywords: [repo.slug, repo.provider, repo.display_name ?? ""],
      repo,
    })),
    query,
  ).map((o) => o.repo);
}
