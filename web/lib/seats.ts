/**
 * Seats: how many people one Celmis holds, against how many it may.
 *
 * The rule lives in the API (src/users/seats.py): five people without a
 * licence, five of them through single sign-on (a count that exists only in a
 * build that has single sign-on), and a licence lifts both. This file is only
 * the web's half of two things:
 *
 *   - the refusal. A sign-in that would add a person beyond a limit is answered
 *     `403 {"detail": {"code": "seat_limit_reached" | "sso_seat_limit_reached",
 *     "message": ...}}`. NextAuth cannot carry a sentence through a redirect,
 *     only a short code, so auth.ts passes the code and the sign-in page says
 *     it in the viewer's language (`auth.seatLimit.*`) rather than in the
 *     API's English. The refusal names the limit and never the head-count: the
 *     person turned away is not signed in.
 *   - the read-only counts the admin card and the invite dialog show
 *     (`GET /api/admin/seats`, `GET /api/invites/seats`).
 *
 * Nobody who already has an account is ever refused; only a NEW person is.
 */

export const SEAT_LIMIT_CODES = ["seat_limit_reached", "sso_seat_limit_reached"] as const;
export type SeatLimitCode = (typeof SEAT_LIMIT_CODES)[number];

/** Refusal code → the i18n key that says it. */
export const SEAT_LIMIT_MESSAGE_KEY: Record<SeatLimitCode, string> = {
  seat_limit_reached: "auth.seatLimit.seats",
  sso_seat_limit_reached: "auth.seatLimit.sso",
};

export function isSeatLimitCode(value: unknown): value is SeatLimitCode {
  return typeof value === "string" && (SEAT_LIMIT_CODES as readonly string[]).includes(value);
}

/** The refusal code in an API error body, or null when the body is any other
 *  error. Only the code is read: the sentence is the API's, not ours to show. */
export function seatLimitCodeOf(body: unknown): SeatLimitCode | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const code = (detail as { code?: unknown }).code;
  return isSeatLimitCode(code) ? code : null;
}

/** Used and limit; `limit` is null when unlimited. */
export interface SeatCount {
  used: number;
  limit: number | null;
}

/** `GET /api/admin/seats` (global admins). */
export interface SeatsState {
  seats: SeatCount;
  /** Null in a build without single sign-on: there is nothing to count. */
  sso_seats: SeatCount | null;
  /** A licence is in force (it may or may not lift the limits). */
  licensed: boolean;
  /** The free allowances: "up to N people without a licence". */
  free_seats: number;
  free_sso_seats: number | null;
}

/** `GET /api/invites/seats` (people who may invite into a team workspace): a yes
 *  or no and nothing more. */
export interface FreeSeat {
  has_free_seat: boolean;
}

/** Is this count past its limit? An installation can be: it had more people
 *  when the limit arrived, or a licence ran out. Nobody is switched off then;
 *  only adding stops. */
export function isOver(count: SeatCount): boolean {
  return count.limit !== null && count.used > count.limit;
}

export function isFull(count: SeatCount): boolean {
  return count.limit !== null && count.used >= count.limit;
}

/** Any count of this installation past its limit. */
export function anyOver(state: SeatsState): boolean {
  return isOver(state.seats) || (state.sso_seats !== null && isOver(state.sso_seats));
}

/** Any count of this installation at its limit. */
export function anyFull(state: SeatsState): boolean {
  return isFull(state.seats) || (state.sso_seats !== null && isFull(state.sso_seats));
}
