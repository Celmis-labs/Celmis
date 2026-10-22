"use client";

import { useEffect } from "react";

import { followSystemTheme } from "@/lib/theme";

/** Keeps the theme following the system (and the other tabs) after the first
 *  paint — see `followSystemTheme`.
 *
 *  Mounted once in the root layout rather than inside the toggle, which is
 *  absent from some pages (an invite, a password reset) that must still follow
 *  the system. Renders nothing. */
export function ThemeSync() {
  useEffect(() => followSystemTheme(), []);
  return null;
}
