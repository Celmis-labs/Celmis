/** What the Celmis agent remembers in this browser, and when it forgets it.
 *
 *  The conversation's session id and whether the floating panel is open both
 *  live in localStorage, which outlives a sign-out and a workspace switch.
 *  History is filtered by workspace on the server, so nothing leaks — but the
 *  next person on a shared browser, or the same person in another workspace,
 *  would reopen a panel bound to the previous sitting and append their
 *  questions to a session id that spans users or workspaces. Both are cleared
 *  on every sign-out and every workspace switch; each of those ends in a full
 *  page load, so no in-memory copy survives either.
 *
 *  A plain module so the shell, the settings page and the agent's own
 *  components share the key names instead of each spelling them. */
export const AGENT_SESSION_KEY = "automation_session_id";
export const AGENT_WIDGET_OPEN_KEY = "celmis:agent-widget";

export function forgetAgentSession(): void {
  try {
    localStorage.removeItem(AGENT_SESSION_KEY);
    localStorage.removeItem(AGENT_WIDGET_OPEN_KEY);
  } catch {
    /* private mode — nothing was stored */
  }
}
