import { type NextRequest } from "next/server";

import { GET as answer, HEAD as answerHead, OPTIONS } from "./[...path]/route";

/**
 * `/.well-known` itself, with nothing after it: the same JSON 404 as any other
 * path nothing is published at. Without this file Next answers the bare path
 * with its HTML 404 page, because the catch-all next door needs one segment.
 * (A trailing slash, `/.well-known/`, is normalised by Next to this path with a
 * 308 before any handler runs; the answer it lands on is this one.)
 */
export const dynamic = "force-dynamic";

const none = { params: Promise.resolve({ path: [] as string[] }) };

export async function GET(req: NextRequest): Promise<Response> {
  return answer(req, none);
}

export async function HEAD(req: NextRequest): Promise<Response> {
  return answerHead(req, none);
}

export { OPTIONS };
