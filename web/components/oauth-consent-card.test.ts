import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OAuthConsentCard } from "./oauth-consent-card";
import type { ConsentInfo } from "@/lib/oauth-consent";

const info = (over: Partial<ConsentInfo> = {}): ConsentInfo => ({
  client: { client_id: "ec_1", name: "Acme Agent", redirect_host: "localhost", registered_by_administrator: false },
  workspace: { id: "w1", slug: "ws-a", name: "Alpha", role: "owner" },
  workspaces: [{ id: "w1", slug: "ws-a", name: "Alpha", role: "owner" }],
  scopes: [{ scope: "read:graph", granted: true, reason: null }],
  granted: ["read:graph"],
  blocked: null,
  ...over,
});

const render = (i: ConsentInfo) =>
  renderToStaticMarkup(createElement(OAuthConsentCard, {
    info: i, workspace: "ws-a", onWorkspace: () => {}, busy: false, error: null,
    onAllow: () => {}, onDeny: () => {},
  }));

describe("OAuthConsentCard", () => {
  it("shows the app, the host it returns to, the access, and both buttons", () => {
    const html = render(info());
    expect(html).toContain("Acme Agent");
    expect(html).toContain("localhost");
    expect(html).toContain('data-testid="oauth-allow"');
    expect(html).toContain('data-testid="oauth-deny"');
    expect(html).toContain('data-testid="oauth-scopes"');
  });
  it("renders the app name as text, never as markup", () => {
    const html = render(info({ client: { client_id: "x", name: "<img src=x onerror=alert(1)>", redirect_host: "h", registered_by_administrator: false } }));
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("&lt;img");
  });
  it("offers a workspace picker only when there is a choice", () => {
    expect(render(info())).not.toContain('data-testid="oauth-workspace"');
    const two = info({ workspaces: [
      { id: "w1", slug: "ws-a", name: "Alpha", role: "owner" },
      { id: "w2", slug: "ws-b", name: "Bravo", role: "member" },
    ] });
    expect(render(two)).toContain('data-testid="oauth-workspace"');
  });
  it("disables Allow when the server says this person may not connect", () => {
    const html = render(info({ blocked: "Ask an administrator." }));
    expect(html).toContain("Ask an administrator.");
    expect(html).toMatch(/data-testid="oauth-allow"[^>]*disabled|disabled[^>]*data-testid="oauth-allow"/);
  });
});
