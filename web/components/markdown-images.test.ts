import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { imageAsText } from "@/lib/markdown-image";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => {} }) }));

const { Markdown } = await import("./markdown");

const render = (text: string, images?: "show" | "alt") =>
  renderToStaticMarkup(createElement(Markdown, { text, images }));

const REMOTE = "![diagram](https://tracker.example/pixel.png?u=viewer)";

describe("a markdown image shown as text", () => {
  it("is loaded as an image by default, as every other page of the app expects", () => {
    expect(render(REMOTE)).toContain('<img src="https://tracker.example/pixel.png?u=viewer"');
  });

  it("is not an image at all in a pack: no img element, no src, nothing to fetch", () => {
    const html = render(REMOTE, "alt");
    expect(html).not.toMatch(/<img/i);
    expect(html).not.toMatch(/\bsrc=/i);
    expect(html).toContain("diagram");
  });

  it("keeps the alt text and offers a link that only a click follows", () => {
    const html = render(REMOTE, "alt");
    expect(html).toContain('<a href="https://tracker.example/pixel.png?u=viewer"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
  });

  it("does not put a link inside a link", () => {
    const html = render("[![logo](https://tracker.example/logo.png)](https://docs.example/start)", "alt");
    expect(html).not.toMatch(/<img/i);
    expect(html.match(/<a /g)).toHaveLength(1);
    expect(html).toContain('href="https://docs.example/start"');
    expect(html).toContain("logo");
  });

  it("shows a relative or data image as text alone, never as a link", () => {
    for (const src of ["./img/a.png", "/img/a.png", "data:image/png;base64,AAAA"]) {
      const html = render(`![local picture](${src})`, "alt");
      expect(html).not.toMatch(/<img|<a /i);
      expect(html).toContain("local picture");
    }
  });

  it("shows a reference-style image and one with no alt text without loading them", () => {
    const html = render("![][pic]\n\n[pic]: https://tracker.example/files/chart.svg", "alt");
    expect(html).not.toMatch(/<img/i);
    expect(html).toContain(">chart.svg<");
  });

  it("leaves everything else in the text as it was", () => {
    const html = render("# Title\n\nText with `code` and a [link](https://example.org/a).\n\n- one\n- two", "alt");
    expect(html).toContain("<h1");
    expect(html).toContain("<code");
    expect(html).toContain('href="https://example.org/a"');
    expect(html).toContain("<li>one</li>");
  });

  it("does not run an HTML image written in the text", () => {
    const html = render('<img src="https://tracker.example/p.png" onerror="alert(1)">', "alt");
    expect(html).not.toMatch(/<img/i);
  });
});

describe("imageAsText", () => {
  it("prefers the alt text, squeezed to one line", () => {
    expect(imageAsText("https://a.example/x.png", "  two\n lines ")).toEqual({
      text: "two lines", href: "https://a.example/x.png",
    });
  });

  it("falls back to the file name, then the host", () => {
    expect(imageAsText("https://a.example/img/My%20Chart.png", "").text).toBe("My Chart.png");
    expect(imageAsText("https://a.example/", "").text).toBe("a.example");
  });

  it("links only an absolute http(s) address", () => {
    for (const src of ["", "a.png", "//a.example/x.png", "javascript:alert(1)", "data:image/png;base64,AA", "ftp://a.example/x"]) {
      expect(imageAsText(src, "alt").href).toBeNull();
    }
    expect(imageAsText("http://a.example/x.png", "alt").href).toBe("http://a.example/x.png");
  });

  it("has nothing to show for an image with neither alt text nor an address", () => {
    expect(imageAsText("./a.png", "")).toEqual({ text: "", href: null });
  });
});
