/**
 * Acoustic signatures.
 *
 * Each entry is a coarse parameterisation of how a language's speech looks on a
 * spectrogram: where its formants sit, how fast its syllables come, how much its
 * pitch glides, how percussive its consonants are. These numbers drive the 3D
 * voice field, so switching language in the demo visibly reshapes the terrain —
 * which is the honest version of the "90+ languages, native dialect" claim.
 *
 * They are stylised, not measured. Enough to be true in character.
 */

export type Signature = {
  /** BCP-47-ish tag */
  code: string;
  /** Endonym — how speakers write the language themselves */
  native: string;
  /** English label */
  label: string;
  /** Dialect / regional variant offered for this language */
  dialect: string;
  /** syllables per second, normalised to the field's time axis */
  rate: number;
  /** normalised formant centres, 0 = low frequency, 1 = high */
  f1: number;
  f2: number;
  f3: number;
  /** pitch/formant glide — high for tonal languages */
  glide: number;
  /** plosive & retroflex burst energy */
  burst: number;
  /** formant bandwidth — high for breathier, more open vowels */
  breadth: number;
};

export const SIGNATURES: Signature[] = [
  {
    code: "en-IN",
    native: "English",
    label: "English",
    dialect: "Indian",
    rate: 5.2,
    f1: 0.17,
    f2: 0.44,
    f3: 0.72,
    glide: 0.35,
    burst: 0.55,
    breadth: 0.95,
  },
  {
    code: "hi-IN",
    native: "हिन्दी",
    label: "Hindi",
    dialect: "Dilliwali",
    rate: 5.9,
    f1: 0.15,
    f2: 0.41,
    f3: 0.69,
    glide: 0.28,
    burst: 0.82,
    breadth: 0.88,
  },
  {
    code: "ta-IN",
    native: "தமிழ்",
    label: "Tamil",
    dialect: "Chennai",
    rate: 6.4,
    f1: 0.13,
    f2: 0.47,
    f3: 0.74,
    glide: 0.22,
    burst: 0.74,
    breadth: 0.8,
  },
  {
    code: "te-IN",
    native: "తెలుగు",
    label: "Telugu",
    dialect: "Telangana",
    rate: 6.2,
    f1: 0.16,
    f2: 0.46,
    f3: 0.73,
    glide: 0.26,
    burst: 0.72,
    breadth: 0.86,
  },
  {
    code: "ar-AE",
    native: "العربية",
    label: "Arabic",
    dialect: "Khaleeji",
    rate: 4.7,
    f1: 0.1,
    f2: 0.33,
    f3: 0.63,
    glide: 0.3,
    burst: 0.6,
    breadth: 1.18,
  },
  {
    code: "zh-CN",
    native: "中文",
    label: "Mandarin",
    dialect: "Putonghua",
    rate: 4.9,
    f1: 0.19,
    f2: 0.52,
    f3: 0.78,
    glide: 0.95,
    burst: 0.38,
    breadth: 0.72,
  },
  {
    code: "es-MX",
    native: "Español",
    label: "Spanish",
    dialect: "Mexicano",
    rate: 6.8,
    f1: 0.16,
    f2: 0.46,
    f3: 0.71,
    glide: 0.2,
    burst: 0.48,
    breadth: 0.85,
  },
  {
    code: "pt-BR",
    native: "Português",
    label: "Portuguese",
    dialect: "Brasileiro",
    rate: 6.1,
    f1: 0.2,
    f2: 0.43,
    f3: 0.7,
    glide: 0.42,
    burst: 0.44,
    breadth: 1.05,
  },
  {
    code: "ja-JP",
    native: "日本語",
    label: "Japanese",
    dialect: "Tokyo",
    rate: 7.2,
    f1: 0.14,
    f2: 0.49,
    f3: 0.76,
    glide: 0.5,
    burst: 0.34,
    breadth: 0.66,
  },
  {
    code: "de-DE",
    native: "Deutsch",
    label: "German",
    dialect: "Hochdeutsch",
    rate: 5.0,
    f1: 0.12,
    f2: 0.38,
    f3: 0.66,
    glide: 0.16,
    burst: 0.78,
    breadth: 0.78,
  },
  {
    code: "fr-FR",
    native: "Français",
    label: "French",
    dialect: "Métropolitain",
    rate: 5.6,
    f1: 0.21,
    f2: 0.55,
    f3: 0.8,
    glide: 0.24,
    burst: 0.4,
    breadth: 0.92,
  },
  {
    code: "bn-IN",
    native: "বাংলা",
    label: "Bengali",
    dialect: "Kolkata",
    rate: 5.7,
    f1: 0.18,
    f2: 0.4,
    f3: 0.68,
    glide: 0.33,
    burst: 0.7,
    breadth: 1.0,
  },
  {
    code: "id-ID",
    native: "Bahasa",
    label: "Indonesian",
    dialect: "Jakarta",
    rate: 6.0,
    f1: 0.15,
    f2: 0.45,
    f3: 0.73,
    glide: 0.18,
    burst: 0.5,
    breadth: 0.9,
  },
];

export const DEFAULT_SIGNATURE = SIGNATURES[0];

export const byCode = (code: string): Signature =>
  SIGNATURES.find((s) => s.code === code) ?? DEFAULT_SIGNATURE;

/** Total languages the platform supports — the picker shows a curated subset. */
export const TOTAL_LANGUAGES = 94;
