import { redirect } from "next/navigation";

import { legacyAgentHref } from "@/lib/review-settings-routes";

/** The workspace agent prompts are the Prompts section of the Global scope
 *  on /review-settings now. */
export default function AgentsRedirect() {
  redirect(legacyAgentHref(null));
}
