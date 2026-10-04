import { redirect } from "next/navigation";

import { legacyDefaultsHref, type LegacySearchParams } from "@/lib/review-settings-routes";

/** The workspace review defaults are the Global scope of /review-settings
 *  now; `?tab=` lands on the section that holds that tab's settings. */
export default async function ReviewDefaultsRedirect({
  searchParams,
}: {
  searchParams: Promise<LegacySearchParams>;
}) {
  redirect(legacyDefaultsHref(await searchParams));
}
