/**
 * Review categories: which agent's finding is which kind of finding.
 *
 * The web half of `src/review/categories.py` — the same agent → category
 * map, spelled once here for every page that groups or labels findings
 * (Kodus's "Bug / Performance / Security / Business logic"). A test reads
 * both files and refuses a drift between them.
 */

export const AGENT_CATEGORY: Record<string, string> = {
  defect: "Bug",
  contract: "Contract",
  security: "Security",
  performance: "Performance",
  business_logic: "Business logic",
  compliance: "Compliance",
  breaking_change: "Breaking change",
  structural: "Structure",
  cve: "Dependencies",
};

/** How an agent's name is shown. The wire names are snake_case
 *  (`business_logic`), which reads badly as a heading. */
export const AGENT_LABEL: Record<string, string> = {
  defect: "Defect",
  contract: "Contract",
  security: "Security",
  performance: "Performance",
  business_logic: "Business logic",
  verifier: "Verifier",
  compliance: "Compliance",
  structural: "Structural",
  cve: "CVE",
  breaking_change: "Breaking change",
};

export const OTHER_CATEGORY = "Other";

/** The category of a finding's agent; a merged finding names several agents
 *  comma-joined and is filed under the first one that has a category. */
export function categoryOfAgent(agent: string | null | undefined): string {
  for (const name of (agent ?? "").split(",")) {
    const label = AGENT_CATEGORY[name.trim().toLowerCase()];
    if (label) return label;
  }
  return OTHER_CATEGORY;
}

/** The label an agent name is shown with; unknown names pass through. */
export function agentLabel(agent: string): string {
  return AGENT_LABEL[agent] ?? agent;
}
