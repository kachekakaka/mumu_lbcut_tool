/// <reference types="vite/client" />
/// <reference types="vitest/config" />

declare const __MOCK__: boolean

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
