import { type ComponentProps, type ComponentType, createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { I18nProvider, useT } from "./index";
import { LOCALES, type Locale } from "./locales";
import { serverDictionary } from "./server-dict";

const KEY = "review.settings.thresholdLabel";

// The provider's props as the root layout passes them; `children` is given to
// `createElement` as its own argument, as the lint rule asks.
const Provider = I18nProvider as unknown as ComponentType<
  Partial<Omit<ComponentProps<typeof I18nProvider>, "children">>
>;

function Probe() {
  return createElement("span", null, useT()(KEY));
}

/** The HTML the server sends for a visitor whose language is `locale`. */
async function firstPaint(locale: Locale): Promise<string> {
  const initialDict = await serverDictionary(locale);
  return renderToString(
    createElement(Provider, { initialLocale: locale, initialDict }, createElement(Probe)),
  );
}

describe("the first paint is in the visitor's language", () => {
  it("sends nothing for the two languages the client bundle carries", async () => {
    expect(await serverDictionary("en")).toBeUndefined();
    expect(await serverDictionary("uk")).toBeUndefined();
  });

  it("loads a dictionary for every other language the interface offers", async () => {
    for (const locale of LOCALES.filter((l) => l !== "en" && l !== "uk")) {
      const dict = await serverDictionary(locale);
      expect(dict, locale).toBeDefined();
      expect(Object.keys(dict!).length, locale).toBeGreaterThan(100);
    }
  });

  it("renders a language outside the bundle as itself, not as English until a download ends", async () => {
    for (const locale of LOCALES) {
      const dict = (await serverDictionary(locale)) ?? undefined;
      const html = await firstPaint(locale);
      if (dict) expect(html, locale).toContain(dict[KEY].replace(/&/g, "&amp;"));
    }
    // One language spelled out, so the check cannot pass by both sides being English.
    expect(await firstPaint("de")).toContain("Kommentare posten für");
    expect(await firstPaint("en")).not.toContain("Kommentare");
  });

  it("falls back to English when the server has no dictionary, as a failed download does", () => {
    const html = renderToString(
      createElement(Provider, { initialLocale: "de" }, createElement(Probe)),
    );
    expect(html).not.toContain("Kommentare");
  });
});
