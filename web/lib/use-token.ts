"use client";
import { useSession } from "next-auth/react";

/** Returns the FastAPI bearer token from the session, or null. */
export function useToken(): string | null {
  const { data } = useSession();
  return (data?.celmisToken as string | null | undefined) ?? null;
}
