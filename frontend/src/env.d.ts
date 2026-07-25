/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * `"false"` routes every call at the real backend; anything else (including unset) uses the
   * hand-authored fixtures in `src/fixtures`. Default-on until M4 wires the API up.
   */
  readonly VITE_USE_FIXTURES?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
