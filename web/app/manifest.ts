import type { MetadataRoute } from "next";

/**
 * Installing the app is not cosmetic here — it is the precondition for the
 * feature that matters most on a phone. iOS only grants Notification
 * permission to a site added to the Home Screen; in a Safari tab the prompt
 * is refused outright. So "install" is what turns "start an agent and walk
 * away" into something that can actually reach you.
 *
 * start_url points at the session list rather than the dashboard: someone
 * opening this from their Home Screen is coming back to check on work.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Celmis — code intelligence + auto PR review",
    short_name: "Celmis",
    description:
      "Start an AI coding session from your phone, get told when it finishes.",
    start_url: "/claude",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#0f1512",
    theme_color: "#0f1512",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      // A maskable copy so Android does not letterbox the icon inside its
      // own shape mask.
      {
        src: "/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
    shortcuts: [
      { name: "New session", url: "/claude" },
      { name: "Reviews", url: "/reviews" },
    ],
  };
}
