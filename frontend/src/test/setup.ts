import '@testing-library/jest-dom'

// Node 25 exposes an experimental `localStorage` object without the Storage
// methods unless --localstorage-file is configured. Vitest can inherit that
// object instead of JSDOM's implementation, so provide a deterministic test
// storage only when the environment's implementation is incomplete.
if (typeof globalThis.localStorage?.getItem !== 'function') {
  const values = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key)
    },
    setItem: (key, value) => {
      values.set(key, String(value))
    },
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
  })
}
