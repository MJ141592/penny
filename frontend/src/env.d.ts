/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * `"true"` serves every request from the hand-authored fixtures in `src/fixtures`, so the UI
   * can be developed and demoed with no backend running. Anything else (including unset) talks
   * to the real API at `/api` — that is the default, deliberately, so a forgotten env var cannot
   * ship a build that quietly shows invented data.
   */
  readonly VITE_USE_FIXTURES?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
