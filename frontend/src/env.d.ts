/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * `"true"` serves every request from the hand-authored fixtures in `src/fixtures`, so the UI
   * can be developed and demoed with no backend running. Anything else (including unset) talks
   * to the real API at `/api` — that is the default, deliberately, so a forgotten env var cannot
   * ship a build that quietly shows invented data.
   */
  readonly VITE_USE_FIXTURES?: string

  /**
   * Penny's WhatsApp number for the landing page's wa.me links, digits only in E.164 order
   * (`17479429824`). Unset falls back to Penny's real number, hard-coded in landing.tsx.
   */
  readonly VITE_PENNY_WHATSAPP_NUMBER?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
