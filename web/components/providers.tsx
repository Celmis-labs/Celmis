"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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
          {children}
          <Toaster position="top-right" richColors closeButton />
        </QueryClientProvider>
      </I18nProvider>
    </SessionProvider>
  );
}
