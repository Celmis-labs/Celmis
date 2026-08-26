import { redirect } from "next/navigation";

/**
 * /byok is retired.
 *
 * LLM provider keys used to be settable in three places (/connections, /byok
 * and /settings/llm) writing to two different credential slots — so a key
 * saved in one place was invisible to the other surfaces. Keys are now
 * workspace-shared and admin-managed, with a single home: /settings/llm
 * (provider keys + per-surface model profiles).
 */
export default function ByokRedirect() {
  redirect("/settings/llm");
}
