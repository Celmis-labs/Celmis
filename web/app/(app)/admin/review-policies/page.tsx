import { redirect } from "next/navigation";

import { settingsHref } from "@/lib/review-settings-routes";

/** The repository list lives in the left panel of /review-settings now,
 *  searchable, with each repository's override count. */
export default function ReviewPoliciesRedirect() {
  redirect(settingsHref());
}
