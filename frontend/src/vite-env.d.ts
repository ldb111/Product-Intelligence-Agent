/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_PIA_WORKSPACE_ID?: string
  readonly VITE_PIA_PRODUCT_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
