"use client";

/**
 * GET /api/capabilities — what this installation has mounted, and which
 * edition it is (src/api/routers/capabilities.py).
 *
 * The contract, from the server's own docstring: hide something ONLY when the
 * document explicitly says `available: false`. No document, an error, or a
 * key this client does not recognise means "show it" — a capability wrongly
 * reported off hides a working page, which is worse than the 403 a wrongly
 * reported on costs. `featureOff` is the one place that rule is spelled.
 *
 * A token is sent when there is one: the signed-in document adds the
 * deployment mode and the licence customer/expiry (the admin health card
 * reads those); the anonymous one has everything a navigation filter needs.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useToken } from "@/lib/use-token";

export type CapabilityFeature = { available: boolean; pages: string[] };

export type Capabilities = {
  schema_version: number;
  product: string;
  api_version: string;
  /** "community" | "enterprise" (or a label owned by src.deployment). */
  edition: string;
  edition_source: string;
  complete?: boolean;
  license?: {
    features: string[];
    customer: string | null;
    expires_at: string | null;
  };
  features: Record<string, CapabilityFeature>;
  pages: Record<string, boolean>;
  degraded?: boolean;
};

export function useCapabilities() {
  const token = useToken();
  return useQuery({
    queryKey: ["capabilities", token ? "authed" : "anon"],
    queryFn: () => api<Capabilities>("/api/capabilities", { token }),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

/** True only when the server explicitly reports `feature` unavailable. */
export function featureOff(caps: Capabilities | undefined, feature: string): boolean {
  return caps?.features?.[feature]?.available === false;
}

/** Hook form of `featureOff` for the common case. */
export function useFeatureOff(feature: string): boolean {
  return featureOff(useCapabilities().data, feature);
}
