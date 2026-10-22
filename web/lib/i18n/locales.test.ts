import { describe, expect, it } from "vitest";

import {
  DEFAULT_LOCALE, LOCALES, detectLocale, isLocale, localeOfTag, parseAcceptLanguage, pickLocale,
} from "./locales";

describe("LOCALES", () => {
  it("are the sixteen the interface ships, English first", () => {
    expect([...LOCALES].sort()).toEqual(
      ["cs", "de", "en", "es", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ro", "sk", "tr", "uk", "zh"],
    );
    expect(LOCALES[0]).toBe("en");
    expect(DEFAULT_LOCALE).toBe("en");
  });
});

describe("isLocale", () => {
  it("accepts exactly the codes there is a dictionary for", () => {
    for (const l of LOCALES) expect(isLocale(l)).toBe(true);
    for (const v of ["xx", "UK", "uk-UA", "", " uk", "__proto__", "constructor", null, undefined, 3]) {
      expect(isLocale(v)).toBe(false);
    }
  });
});

describe("localeOfTag", () => {
  it("maps every locale's own code and its region variants to the locale", () => {
    const variants: Record<string, string[]> = {
      cs: ["cs-CZ"], de: ["de-AT", "de-CH", "de-DE"], en: ["en-US", "en-GB", "en_AU"],
      es: ["es-MX", "es-419", "es-ES"], fr: ["fr-CA", "fr-BE"], it: ["it-CH"], ja: ["ja-JP"],
      ko: ["ko-KR"], nl: ["nl-BE"], pl: ["pl-PL"], pt: ["pt-BR", "pt-PT", "PT_br"],
      ro: ["ro-MD"], sk: ["sk-SK"], tr: ["tr-TR"], uk: ["uk-UA", "UK"],
      zh: ["zh-CN", "zh-TW", "zh-HK", "zh-Hans", "zh-Hant-TW", "zh-Hans-CN"],
    };
    expect(Object.keys(variants).sort()).toEqual([...LOCALES].sort());
    for (const [locale, tags] of Object.entries(variants)) {
      expect(localeOfTag(locale)).toBe(locale);
      for (const tag of tags) expect(localeOfTag(tag), tag).toBe(locale);
    }
  });

  it("reads only the primary subtag, so a three-letter code is not a two-letter one", () => {
    expect(localeOfTag("ukr")).toBeNull();
    expect(localeOfTag("u")).toBeNull();
    expect(localeOfTag("i-klingon")).toBeNull();
  });

  it("finds nothing in a language there is no dictionary for, or in no language", () => {
    for (const tag of ["sv", "nb-NO", "he", "ru-RU", "*", "", "  ", null, undefined]) {
      expect(localeOfTag(tag), String(tag)).toBeNull();
    }
  });
});

describe("parseAcceptLanguage", () => {
  it("lists a browser's header in the order it was written", () => {
    expect(parseAcceptLanguage("uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"))
      .toEqual(["uk-UA", "uk", "en-US", "en"]);
  });

  it("puts the higher q first, whatever the order of the header", () => {
    expect(parseAcceptLanguage("en;q=0.5, de;q=0.9, fr")).toEqual(["fr", "de", "en"]);
  });

  it("keeps the header's order between equal q", () => {
    expect(parseAcceptLanguage("pl;q=0.8, cs;q=0.8, sk;q=0.8")).toEqual(["pl", "cs", "sk"]);
  });

  it("drops a language the browser refuses with q=0, and a q that is not a number counts as 1", () => {
    expect(parseAcceptLanguage("de;q=0, fr;q=0.0, es;q=abc, it;q=")).toEqual(["es", "it"]);
  });

  it("clamps a q outside 0..1", () => {
    expect(parseAcceptLanguage("de;q=7, fr;q=-1, es;q=0.5")).toEqual(["de", "es"]);
  });

  it("ignores spaces, empty entries and other parameters", () => {
    expect(parseAcceptLanguage("  , uk ; level=1 ; q=0.9 ,, en ")).toEqual(["en", "uk"]);
  });

  it("is empty for no header", () => {
    expect(parseAcceptLanguage(null)).toEqual([]);
    expect(parseAcceptLanguage(undefined)).toEqual([]);
    expect(parseAcceptLanguage("")).toEqual([]);
  });

  it("reads no more than 32 entries of a header somebody made huge", () => {
    expect(parseAcceptLanguage(Array(500).fill("de").join(","))).toHaveLength(32);
  });
});

describe("pickLocale", () => {
  it("takes the most preferred language there is a dictionary for", () => {
    expect(pickLocale(["uk-UA", "uk", "en-US", "en"])).toBe("uk");
    expect(pickLocale(["pt-BR", "en"])).toBe("pt");
    expect(pickLocale(["zh-CN"])).toBe("zh");
    expect(pickLocale(["ja"])).toBe("ja");
  });

  it("goes by the order of preference, not by which language is better supported", () => {
    expect(pickLocale(["en-US", "uk"])).toBe("en");
    expect(pickLocale(["uk", "en-US"])).toBe("uk");
  });

  it("skips a language with no dictionary rather than falling back to English at once", () => {
    expect(pickLocale(["sv", "de-AT", "en"])).toBe("de");
    expect(pickLocale(["nb", "no", "pl-PL"])).toBe("pl");
  });

  it("is English when nothing matches or nothing is given", () => {
    expect(pickLocale(["sv", "nb", "ru"])).toBe("en");
    expect(pickLocale([])).toBe("en");
    expect(pickLocale(null)).toBe("en");
    expect(pickLocale(undefined)).toBe("en");
  });

  it("works on what navigator.languages and an Accept-Language header give alike", () => {
    const header = "uk-UA,uk;q=0.9,en-US;q=0.8";
    expect(pickLocale(parseAcceptLanguage(header))).toBe(pickLocale(["uk-UA", "uk", "en-US"]));
  });
});

describe("detectLocale", () => {
  it("lets a saved choice win over what the browser prefers", () => {
    expect(detectLocale("de", "uk-UA,uk;q=0.9")).toEqual({ locale: "de", source: "saved" });
    expect(detectLocale("en", "uk")).toEqual({ locale: "en", source: "saved" });
  });

  it("follows the browser when nothing is saved", () => {
    expect(detectLocale(undefined, "uk-UA,uk;q=0.9,en;q=0.5")).toEqual({ locale: "uk", source: "browser" });
    expect(detectLocale(null, "pt-BR")).toEqual({ locale: "pt", source: "browser" });
  });

  it("is English, from the browser, when none of its languages has a dictionary", () => {
    expect(detectLocale(undefined, "sv-SE,nb;q=0.8")).toEqual({ locale: "en", source: "browser" });
  });

  it("does not take a cookie that is not a locale for a choice", () => {
    expect(detectLocale("xx", "ko-KR")).toEqual({ locale: "ko", source: "browser" });
    expect(detectLocale("", "fr")).toEqual({ locale: "fr", source: "browser" });
    expect(detectLocale("uk-UA", "fr")).toEqual({ locale: "fr", source: "browser" });
  });

  it("says so when the request carried no preference at all, so the client may ask", () => {
    expect(detectLocale(undefined, undefined)).toEqual({ locale: "en", source: "none" });
    expect(detectLocale("xx", "")).toEqual({ locale: "en", source: "none" });
    expect(detectLocale(undefined, "*;q=0")).toEqual({ locale: "en", source: "none" });
  });
});
