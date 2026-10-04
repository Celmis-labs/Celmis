import { redirect } from "next/navigation";

import { legacyAgentHref } from "@/lib/review-settings-routes";

/** One agent's prompt: the Prompts section of Global, opened on that agent. */
export default async function AgentRedirect({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  redirect(legacyAgentHref(decodeURIComponent(name)));
}
