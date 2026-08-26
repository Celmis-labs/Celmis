/**
 * Web-push subscription, client half.
 *
 * The interesting part is not the API calls — it is telling the four
 * different "no" answers apart, because they need four different things from
 * the user and a single "notifications unavailable" would strand them:
 *
 *   insecure  — the page is plain HTTP. Service workers and Push are
 *               secure-context only, so nothing here can work until the
 *               deployment is on HTTPS.
 *   ios-tab   — iOS Safari refuses permission to a browser tab. The site has
 *               to be added to the Home Screen first. This is the one people
 *               hit and cannot guess.
 *   denied    — permission was refused; the browser will not ask again, so
 *               it has to be changed in site settings.
 *   unsupported — the browser has no Push API at all.
 */

export type PushState =
  | "ready"
  | "subscribed"
  | "denied"
  | "insecure"
  | "ios-tab"
  | "unsupported"
  | "server-off";

/** Installed to the Home Screen / opened as an app rather than a tab. */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.matchMedia?.("(display-mode: standalone)").matches ||
    // iOS predates the display-mode media query for this.
    (window.navigator as { standalone?: boolean }).standalone === true
  );
}

function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    // iPadOS reports itself as a Mac; the touch points give it away.
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
  );
}

export function localState(): PushState | null {
  if (typeof window === "undefined") return null;
  if (!window.isSecureContext) return "insecure";
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    // On iOS this is what a plain tab looks like — the APIs only appear once
    // the site is installed, so name that rather than calling it unsupported.
    return isIOS() && !isStandalone() ? "ios-tab" : "unsupported";
  }
  if (Notification.permission === "denied") return "denied";
  return "ready";
}

/**
 * VAPID keys travel as base64url; PushManager wants raw bytes.
 *
 * Typed as ArrayBuffer rather than Uint8Array on purpose: lib.dom types
 * applicationServerKey as BufferSource over a plain ArrayBuffer, which a
 * Uint8Array<ArrayBufferLike> does not satisfy.
 */
function urlBase64ToBytes(base64: string): ArrayBuffer {
  const padded = base64.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

async function register(): Promise<ServiceWorkerRegistration> {
  const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  // `register()` resolves before the worker is usable; pushManager on a
  // not-yet-active registration throws.
  await navigator.serviceWorker.ready;
  return reg;
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (localState() !== "ready" && localState() !== "subscribed") return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return (await reg?.pushManager.getSubscription()) ?? null;
}

/**
 * Ask for permission and register the device. Must be called from a click —
 * browsers ignore a permission request without a user gesture.
 */
export async function enablePush(
  publicKey: string,
  post: (body: unknown) => Promise<unknown>,
): Promise<PushSubscription> {
  const state = localState();
  if (state && state !== "ready") throw new Error(state);

  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("denied");

  const reg = await register();
  const existing = await reg.pushManager.getSubscription();
  // Re-subscribing under a rotated VAPID key fails unless the old one goes
  // first; the endpoint is keyed to the application server key.
  const sub =
    existing ??
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToBytes(publicKey),
    }));

  await post(sub.toJSON());
  return sub;
}

export async function disablePush(
  del: (body: unknown) => Promise<unknown>,
): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  // Server first: if the browser drops it and the DELETE then fails, the row
  // lingers and keeps receiving pushes for a device that will never show them.
  await del({ endpoint: sub.endpoint });
  await sub.unsubscribe();
}
