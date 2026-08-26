/**
 * Copy to clipboard where `navigator.clipboard` may not exist.
 *
 * The Clipboard API is restricted to secure contexts. This deployment is
 * served over plain HTTP on a bare IP, so `navigator.clipboard` is `undefined`
 * there and `navigator.clipboard.writeText(...)` throws a TypeError before it
 * ever reaches a `.catch()` — which is why the copy button silently did
 * nothing, toast included.
 *
 * Returns whether the text made it to the clipboard, so the caller can tell
 * the user to select it by hand instead of claiming a success that did not
 * happen.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied, or a non-focused document. Fall through — the legacy
    // path works in some of the cases the modern one refuses.
  }
  return legacyCopy(text);
}

/**
 * `document.execCommand("copy")` over a temporary selection.
 *
 * Deprecated, and the only thing available on a non-secure origin. It copies
 * the current selection, so the text has to be in the document and selected
 * first — hence the off-screen textarea.
 */
function legacyCopy(text: string): boolean {
  if (typeof document === "undefined") return false;
  const area = document.createElement("textarea");
  area.value = text;
  // Off-screen rather than display:none — a hidden element cannot be selected,
  // and `readOnly` stops the mobile keyboard from appearing for the instant it
  // is focused.
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "-9999px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}
