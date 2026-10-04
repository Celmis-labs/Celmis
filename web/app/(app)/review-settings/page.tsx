import { Suspense } from "react";

import { ReviewSettings } from "@/components/review-settings/review-settings";

/**
 * /review-settings — every code review setting, Global and per repository.
 * The scope and section live in the query string (?repo=&section=&agent=),
 * read with useSearchParams, which wants a Suspense boundary to prerender.
 */
export default function ReviewSettingsPage() {
  return (
    <Suspense fallback={null}>
      <ReviewSettings />
    </Suspense>
  );
}
