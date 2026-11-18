import { type ComponentProps, type ComponentType, createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { I18nProvider } from "@/lib/i18n";

import { ConfigPicker, ageWords } from "./config-picker";

// `children` goes to `createElement` as its own argument, as the lint rule asks.
const Provider = I18nProvider as unknown as ComponentType<
  Partial<Omit<ComponentProps<typeof I18nProvider>, "children">>
>;

describe("ageWords", () => {
  it("reads the index's compact age in the reader's language", () => {
    expect(ageWords("14d", "en")).toBe("14 days ago");
    expect(ageWords("3h", "en")).toBe("3 hours ago");
    expect(ageWords("1d", "en")).toBe("yesterday");
    expect(ageWords("14d", "uk")).toBe("14 днів тому");
  });

  it("says nothing for an age it does not know", () => {
    expect(ageWords("?", "en")).toBe("");
    expect(ageWords("", "uk")).toBe("");
  });
});

describe("ConfigPicker, closed", () => {
  const erp = {
    slug: "erp", name: "acme/erp", ref: "v2@faad7ac0", age: "14d", upload: true, shared_from: "R&D",
  };
  const render = (selected: typeof erp | null) =>
    renderToStaticMarkup(createElement(
      Provider, { initialLocale: "en" },
      createElement(ConfigPicker, {
        value: selected?.slug ?? "", selected, onPick: () => {}, query: "", onQuery: () => {},
        options: [erp], loading: false,
      }),
    ));

  it("is one field that shows the choice, its revision, age and owner", () => {
    const html = render(erp);
    expect(html.match(/<input/g)).toHaveLength(1);
    expect(html).toContain('role="combobox"');
    expect(html).toContain('value="acme/erp"');
    expect(html).toContain("v2@faad7ac0 · 14 days ago");
    expect(html).toContain("shared by R&amp;D");
    expect(html).not.toContain("14d");
    expect(html).not.toContain('role="listbox"');
  });

  it("shows the no-configuration choice when nothing is picked", () => {
    expect(render(null)).toContain('value="None (static checks only)"');
  });
});
