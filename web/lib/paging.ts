/**
 * Offset paging that cannot strand someone past the last page.
 *
 * A list shrinks under the reader: on the last page of open issues, closing
 * its only row leaves `offset` pointing past the end. The refetch then comes
 * back with no items and a non-zero total, the page shows the "nothing here"
 * state and — because the pager is drawn only beside rows — no way back.
 *
 * No imports, so the tests can compile this file on its own.
 */

/** Where to move a page that came back empty though rows exist before it,
 *  or null when the page is fine as it is. */
export function clampedOffset(
  offset: number, total: number, itemCount: number, pageSize: number,
): number | null {
  if (itemCount > 0 || offset <= 0) return null;
  if (total <= 0) return 0;
  const last = Math.floor((total - 1) / pageSize) * pageSize;
  return last < offset ? last : null;
}
