import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CheckList } from "@/components/check-list";
import { Combobox } from "@/components/combobox";

const people = [
  { value: "1", label: "alice@example.com", hint: "Alice Smith" },
  { value: "2", label: "bob@example.com", hint: "Bob Jones" },
];

const render = (props: Record<string, unknown>) =>
  renderToStaticMarkup(createElement(Combobox, {
    options: people, selected: null, onSelect: () => {}, emptyText: "Nobody found",
    clearLabel: "Clear", defaultOpen: true, ...props,
  }));

describe("Combobox markup", () => {
  it("is one input with combobox semantics and a listbox right under it", () => {
    const html = render({});
    expect(html).toContain('role="combobox"');
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('aria-autocomplete="list"');
    expect(html).toContain('role="listbox"');
    expect(html.match(/role="option"/g)).toHaveLength(2);
    expect(html).toContain("alice@example.com");
    expect(html).toContain("Alice Smith");
    // the highlighted row is named by aria-activedescendant, which points at a real option
    const id = /aria-activedescendant="([^"]+)"/.exec(html)?.[1];
    expect(id).toBeTruthy();
    expect(html).toContain(`id="${id}"`);
  });
  it("says so when nobody is found", () => {
    const html = render({ options: [] });
    expect(html).toContain("Nobody found");
    expect(html).not.toContain('role="option"');
    expect(html).not.toContain("aria-activedescendant");
  });
  it("shows the chosen person in the field, with a way to clear", () => {
    const html = render({
      selected: people[0], defaultOpen: false,
      format: (o: { label: string; hint?: string }) => `${o.hint} · ${o.label}`,
    });
    expect(html).toContain('value="Alice Smith · alice@example.com"');
    expect(html).toContain('aria-label="Clear"');
    expect(html).not.toContain('role="listbox"');
  });
});

describe("CheckList markup", () => {
  const items = ["acme/api", "acme/ui", "acme/shop"].map((slug) => ({ slug }));
  const list = (picked: string[]) => renderToStaticMarkup(createElement(CheckList<{ slug: string }>, {
    items, getId: (r) => r.slug, searchText: (r) => r.slug, picked, onChange: () => {},
    renderItem: (r) => createElement("span", null, r.slug),
    selectAllLabel: "Select all",
    counterLabel: (n: number, m: number) => `${n} of ${m} selected`,
    filterPlaceholder: "Filter", noMatchText: "No match",
  }));
  it("shows the select-all box and the counter", () => {
    expect(list([])).toContain("0 of 3 selected");
    expect(list(["acme/ui"])).toContain("1 of 3 selected");
    expect(list(["acme/ui"])).toContain('aria-checked="mixed"');
    const all = list(["acme/api", "acme/ui", "acme/shop"]);
    expect(all).toContain("3 of 3 selected");
    expect(all).toContain('aria-checked="true"');
    expect(list([])).toContain("Select all");
  });
  it("has no filter for a short list", () => {
    expect(list([])).not.toContain('type="search"');
  });
});
