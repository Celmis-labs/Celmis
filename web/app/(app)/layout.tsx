import { cookies } from "next/headers";

import { AppShell } from "@/components/app-shell";
import { SIDEBAR_COOKIE } from "@/lib/sidebar";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  // Read here, on the server, so the sidebar's first paint is already the
  // width the person left it at — see SIDEBAR_COOKIE.
  const sidebar = (await cookies()).get(SIDEBAR_COOKIE)?.value;
  return <AppShell initialSidebarOpen={sidebar !== "closed"}>{children}</AppShell>;
}
