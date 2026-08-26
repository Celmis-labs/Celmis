/*
 * Celmis service worker — exists for one reason: to receive a push after the
 * tab is gone.
 *
 * Deliberately not a cache/offline worker. Caching an authenticated,
 * fast-moving app would serve stale review results and stale session state,
 * which is worse than a spinner. If offline support is ever wanted it belongs
 * in a separate, explicitly-scoped strategy — not bolted on here.
 */

self.addEventListener("install", () => {
  // Take over immediately: the whole point is to be live for the next push,
  // not after the user happens to close every tab.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  // A push with no readable payload still has to show something: browsers
  // revoke the permission of a worker that receives a push and shows nothing.
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }

  const title = data.title || "Celmis";
  const options = {
    body: data.body || "",
    // tag collapses repeats for the same session instead of stacking them.
    tag: data.tag || "celmis",
    data: { url: data.url || "/claude" },
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    timestamp: Date.now(),
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/claude";

  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // Reuse an open window rather than piling up new ones — on a phone
      // every tap otherwise leaves another copy of the app behind.
      for (const client of all) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client) {
            try {
              await client.navigate(target);
            } catch {
              /* cross-origin or already there — focusing is enough */
            }
          }
          return;
        }
      }
      await self.clients.openWindow(target);
    })(),
  );
});
