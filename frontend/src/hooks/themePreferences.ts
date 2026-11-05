import { useSyncExternalStore } from 'react'

export type ThemeFamily = 'classic' | 'foundry'
export type ThemeMode = 'dark' | 'light' | 'high-contrast'
export const FAMILY_KEY = 'odin-theme-family'
export const MODE_KEY = 'odin-theme'
export const FAMILY_EVENT = 'odin-theme-family-change'
export function readPreference(key: string) {
  try { return localStorage.getItem(key) } catch { return null }
}
export function writePreference(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* Still usable without storage. */ }
}
export function getFamily(): ThemeFamily {
  return readPreference(FAMILY_KEY) === 'foundry' ? 'foundry' : 'classic'
}
export function getMode(): ThemeMode {
  const stored = readPreference(MODE_KEY)
  if (stored === 'light' || stored === 'dark' || stored === 'high-contrast') return stored
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}
export function applyTheme(mode: ThemeMode, family: ThemeFamily) {
  const root = document.documentElement
  root.classList.toggle('light', mode === 'light')
  root.classList.toggle('high-contrast', mode === 'high-contrast')
  root.dataset.themeFamily = family === 'foundry' && mode === 'dark' ? 'foundry' : 'classic'
}

// Badges need the effective family, without initiating account-theme requests.
function subscribe(listener: () => void) {
  const observer = new MutationObserver(listener)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme-family', 'class'] })
  window.addEventListener(FAMILY_EVENT, listener)
  return () => { observer.disconnect(); window.removeEventListener(FAMILY_EVENT, listener) }
}
export function useFoundry() {
  return useSyncExternalStore(subscribe, () => {
    const root = document.documentElement
    return root.dataset.themeFamily === 'foundry' && !root.classList.contains('light') && !root.classList.contains('high-contrast')
  }, () => false)
}
