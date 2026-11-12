/**
 * Language name lookup.
 *
 * Ported from leadero's src/core/tool/builtin/handlers/language/language.ts.
 * Used to build human-readable source/target language descriptions for the
 * translation prompts (see src/core/translation/prompts.ts) — sending the model
 * "zh-CN" instead of "Simplified Chinese" measurably degrades translation
 * quality on weaker models.
 *
 * Returns the input code unchanged if no friendly name is known — callers
 * rely on this fallback behaviour (the locale key `language-<code>` may still
 * be resolved downstream via getString).
 */

const LANGUAGE_NAMES: Record<string, string> = {
  zh: "Chinese",
  "zh-CN": "Simplified Chinese",
  "zh-TW": "Traditional Chinese",
  "zh-HK": "Traditional Chinese",
  en: "English",
  "en-US": "English",
  "en-GB": "English",
  ja: "Japanese",
  "ja-JP": "Japanese",
  ko: "Korean",
  "ko-KR": "Korean",
  fr: "French",
  "fr-FR": "French",
  de: "German",
  "de-DE": "German",
  "de-AT": "German",
  "de-CH": "German",
  es: "Spanish",
  "es-ES": "Spanish",
  "es-MX": "Spanish",
  "es-AR": "Spanish",
  ru: "Russian",
  "ru-RU": "Russian",
  pt: "Portuguese",
  "pt-BR": "Portuguese",
  "pt-PT": "Portuguese",
  it: "Italian",
  "it-IT": "Italian",
  ar: "Arabic",
  "ar-SA": "Arabic",
  "ar-EG": "Arabic",
  hi: "Hindi",
  "hi-IN": "Hindi",
  th: "Thai",
  "th-TH": "Thai",
  vi: "Vietnamese",
  "vi-VN": "Vietnamese",
  uk: "Ukrainian",
  "uk-UA": "Ukrainian",
  pl: "Polish",
  "pl-PL": "Polish",
  nl: "Dutch",
  "nl-NL": "Dutch",
  tr: "Turkish",
  "tr-TR": "Turkish",
  sv: "Swedish",
  "sv-SE": "Swedish",
  da: "Danish",
  "da-DK": "Danish",
  fi: "Finnish",
  "fi-FI": "Finnish",
  no: "Norwegian",
  nb: "Norwegian",
  nn: "Norwegian",
  "nb-NO": "Norwegian",
  cs: "Czech",
  "cs-CZ": "Czech",
  el: "Greek",
  "el-GR": "Greek",
  he: "Hebrew",
  "he-IL": "Hebrew",
  hu: "Hungarian",
  "hu-HU": "Hungarian",
  ro: "Romanian",
  "ro-RO": "Romanian",
  id: "Indonesian",
  "id-ID": "Indonesian",
  fa: "Persian",
  "fa-IR": "Persian",
  ca: "Catalan",
  sk: "Slovak",
  "sk-SK": "Slovak",
  bg: "Bulgarian",
  "bg-BG": "Bulgarian",
  hr: "Croatian",
  "hr-HR": "Croatian",
  sr: "Serbian",
  sl: "Slovenian",
  lt: "Lithuanian",
  lv: "Latvian",
  et: "Estonian",
  ms: "Malay",
  bn: "Bengali",
  ta: "Tamil",
  la: "Latin",
  eo: "Esperanto",
};

/**
 * Friendly name for a BCP-47 locale code, or the code itself when unknown.
 * Falls back to the primary subtag ("pt-BR" → Portuguese via "pt"); callers
 * rely on unknown codes passing through unchanged.
 */
export function getLanguageName(code: string): string {
  const c = String(code || "").trim();
  if (!c) return c;
  if (LANGUAGE_NAMES[c]) return LANGUAGE_NAMES[c];
  const primary = c.split(/[-_]/)[0]?.toLowerCase();
  return (primary && LANGUAGE_NAMES[primary]) || c;
}

/** The raw mapping, exposed for callers that need to enumerate or extend it. */
export const LANGUAGE_NAME_MAP: Readonly<Record<string, string>> =
  LANGUAGE_NAMES;
