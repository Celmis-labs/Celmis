"use client";

/**
 * AGPL §13 — the source offer, in the interface.
 *
 * Celmis is a network service: people use it through a browser, and §13 of the
 * AGPL says everyone who does is entitled to its source. Until now the running
 * application said nothing at all — no version, no licence, no link — so the
 * obligation was documented in a file that only somebody who already had the
 * source would read.
 *
 * The link points at the SOURCE OF THE RUNNING BUILD, not at the default
 * branch. "Here is our repository" is not the offer; "here is the code you are
 * talking to" is. `api_version` is `0.1.0+<sha>` for a build off a commit and
 * `0.1.0` for a tagged one, so the two cases address differently.
 *
 * It costs one request that is already cached, and every self-hosted instance
 * becomes a link back to the project.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** Where the source lives. One constant, changed once if the repo moves. */
const REPO = "https://github.com/Celmis-labs/Celmis";

function sourceUrl(version: string | undefined): string {
  if (!version) return REPO;
  const [tag, sha] = version.split("+");
  // A build carries the commit it came from; prefer it — it is exact.
  if (sha) return `${REPO}/tree/${sha}`;
  return `${REPO}/releases/tag/v${tag}`;
}

export function LicenseFooter() {
  const caps = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api<{ api_version?: string }>("/api/capabilities"),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const version = caps.data?.api_version;

  return (
    <footer className="mt-8 border-t border-[var(--color-border)] px-4 py-3 text-xs text-[var(--color-muted-foreground)]">
      <span>Celmis{version ? ` ${version}` : ""}</span>
      <span aria-hidden="true"> · </span>
      <a
        className="underline underline-offset-2 hover:text-[var(--color-foreground)]"
        href="https://www.gnu.org/licenses/agpl-3.0.html"
        target="_blank"
        rel="noreferrer"
      >
        AGPL-3.0
      </a>
      <span aria-hidden="true"> · </span>
      <a
        className="underline underline-offset-2 hover:text-[var(--color-foreground)]"
        href={sourceUrl(version)}
        target="_blank"
        rel="noreferrer"
      >
        Source
      </a>
    </footer>
  );
}
