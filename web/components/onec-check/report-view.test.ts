import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ReportView } from "@/components/onec-check/report-view";
import type { ReportBlock } from "@/lib/api";

const html = (blocks: ReportBlock[]) => renderToStaticMarkup(createElement(ReportView, { blocks }));

describe("ReportView", () => {
  it("renders every block kind with inline markup", () => {
    const out = html([
      { type: "title", text: "Report", meta: [["File", "a.erf"]] },
      { type: "para", kind: "callout", tone: "bad", text: "**Verdict**: do not use `X`." },
      { type: "heading", level: 2, text: "1. Sources" },
      { type: "table", header: ["Name", "Code"], rows: [["one", "Foo()"]], widths: [], mono: [1] },
      { type: "code", lines: ["a = 1;", "b = 2;"] },
      { type: "bullets", items: ["first", "second"], ordered: true },
      { type: "page_break" },
    ]);
    expect(out).toContain("<strong>Verdict</strong>");
    expect(out).toContain(">X</code>");
    expect(out).toContain("border-red-500/40");
    expect(out).toMatch(/<td[^>]*font-mono[^>]*>Foo\(\)<\/td>/);
    expect(out).toContain("<pre");
    expect(out).toContain("a = 1;\nb = 2;");
    expect(out).toContain("<ol");
  });

  it("keeps markup characters in a mono cell and escapes HTML", () => {
    const out = html([
      { type: "table", header: ["C"], rows: [["**x**"]], widths: [], mono: [0] },
      { type: "para", kind: "body", tone: "neutral", text: "<script>alert(1)</script>" },
    ]);
    expect(out).toContain("**x**");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });
});
