"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { domAnimation, LazyMotion, MotionConfig } from "motion/react";
import { SessionProvider } from "next-auth/react";
import { useState } from "react";
import { Toaster } from "sonner";

import { I18nProvider, type Locale } from "@/lib/i18n";

export function Providers({
  children,
  initialLocale,
}: {
  children: React.ReactNode;
  initialLocale?: Locale;
}) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: false,
            // Coming back to a backgrounded tab is the normal way a phone is
            // used, and it is exactly when the data on screen is stale.
            // staleTime keeps that from turning into a refetch storm.
            refetchOnWindowFocus: true,
          },
        },
      }),
  );
  return (
    <SessionProvider>
      <I18nProvider initialLocale={initialLocale}>
        <QueryClientProvider client={queryClient}>
          {/* Motion, loaded lean. Components use the `m.*` elements from
              "motion/react-m", which carry no animation code of their own;
              `domAnimation` supplies it once (animate, exit, hover, focus —
              no layout or drag, which nothing here uses), so the bundle pays
              for the features rather than for every component that animates.
              `reducedMotion="user"` drops transforms and layout motion for
              anyone whose system asks for less; opacity still fades. */}
          <LazyMotion features={domAnimation}>
            <MotionConfig reducedMotion="user">
              {children}
            </MotionConfig>
          </LazyMotion>
          {/* Toned by globals.css (Toasts): a card with a tinted icon in either
              theme, instead of sonner's saturated rich colours, which did
              not follow the theme toggle. */}
          <Toaster position="top-right" closeButton />
        </QueryClientProvider>
      </I18nProvider>
    </SessionProvider>
  );
}
