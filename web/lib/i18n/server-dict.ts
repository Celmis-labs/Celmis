/**
 * The dictionary of a language the client bundle does not carry, loaded on the
 * server.
 *
 * `en` and `uk` ship inside the client bundle. The other fourteen are fetched
 * by the browser after the first render, so a visitor whose browser asks for
 * German got `<html lang="de">` around English text, and German only once the
 * download finished. The root layout loads the dictionary for the language it
 * detected and hands it to the provider, so the first paint — the HTML itself
 * — is already in the visitor's language.
 */

import { LOCALES, type Locale } from "./locales";

export type Dict = Record<string, string>;

/** The languages whose dictionaries are part of the client bundle. */
const BUNDLED: readonly Locale[] = ["en", "uk"];

/** The dictionary for `locale`, or undefined when the bundle already has it
 *  (nothing to send) or there is no file for it (the provider falls back to
 *  English, as it does when a lazy load fails). */
export async function serverDictionary(locale: Locale): Promise<Dict | undefined> {
  if (BUNDLED.includes(locale) || !LOCALES.includes(locale)) return undefined;
  try {
    const mod = await import(`./messages/${locale}.json`);
    return mod.default as Dict;
  } catch {
    return undefined;
  }
}
