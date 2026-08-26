import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { cookies } from "next/headers";
import "./globals.css";
import { Providers } from "@/components/providers";
import { DEFAULT_LOCALE, type Locale } from "@/lib/i18n";
import { THEME_INIT_SCRIPT } from "@/components/theme-toggle";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Celmis — Code intelligence + auto PR review",
  description:
    "Multi-agent PR review across GitHub, GitLab, and Bitbucket with cross-repo blast-radius detection.",
  // iOS ignores the manifest's icons for "Add to Home Screen" and reads this
  // instead — and being installed is what unlocks notifications there at all.
  appleWebApp: { capable: true, title: "Celmis", statusBarStyle: "black-translucent" },
  icons: { icon: "/icon-192.png", apple: "/icon-192.png" },
};

// viewportFit=cover lets the fixed nav drawer and the sticky top bar paint
// under the notch and the home indicator; the shell pads itself back out with
// env(safe-area-inset-*). Without it iOS letterboxes the page instead.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const cookieStore = await cookies();
  const locale = (cookieStore.get("locale")?.value as Locale) || DEFAULT_LOCALE;
  return (
    <html
      lang={locale}
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full">
        <Providers initialLocale={locale}>{children}</Providers>
      </body>
    </html>
  );
}
