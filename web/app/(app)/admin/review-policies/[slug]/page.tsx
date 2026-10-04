import { redirect } from "next/navigation";

import { legacyPolicyHref, type LegacySearchParams } from "@/lib/review-settings-routes";

/** A repository's policy is its scope on /review-settings now; `?tab=`
 *  lands on the section that holds that tab's settings. */
export default async function ReviewPolicyRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<LegacySearchParams>;
}) {
  const { slug } = await params;
  redirect(legacyPolicyHref(decodeURIComponent(slug), await searchParams));
}
