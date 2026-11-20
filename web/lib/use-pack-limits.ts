"use client";

import { useQuery } from "@tanstack/react-query";

import { codebaseApi, type PackLimits } from "@/lib/api";
import { UPLOAD_LIMITS } from "@/lib/codebase";
import { useToken } from "@/lib/use-token";

/** What one upload may hold in this workspace. Until the server has answered (or if it
 *  cannot), the built-in numbers stand; the server enforces its own either way. */
export function usePackLimits() {
  const token = useToken();
  const q = useQuery({
    queryKey: ["codebase", "limits"],
    queryFn: () => codebaseApi.limits(token!),
    enabled: !!token,
    staleTime: 60_000,
  });
  const l = q.data?.limits;
  return {
    query: q,
    data: q.data as PackLimits | undefined,
    upload: l
      ? { archiveBytes: l.archive_bytes, files: l.max_files, fileBytes: l.file_bytes, totalBytes: l.total_bytes }
      : UPLOAD_LIMITS,
  };
}
