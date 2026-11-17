import { useDensity } from '../components/foundry/DensityToggle'
import { useState, useEffect, useCallback } from 'react'
import { loadServerTheme, saveServerMode, THEME_EVENT } from './serverTheme'
import { applyTheme, effectiveFamily as resolveFamily, FAMILY_EVENT, FAMILY_KEY, getFamily, getMode, MODE_KEY, readPreference, writePreference, type ThemeFamily, type ThemeMode } from './themePreferences'

/** Classic is the default family. Existing account light/dark sync is retained;
 * family and high-contrast remain device-local until a later backend phase. */
export function useTheme() {
  const [theme, _setTheme] = useState<ThemeMode>(getMode)
  const [family, _setFamily] = useState<ThemeFamily>(getFamily)
  const [density, setDensity] = useDensity()
  useEffect(() => {
    applyTheme(theme, family)
    if (resolveFamily(theme, family) === 'fjord') document.documentElement.dataset.fjordDensity = density
    else delete document.documentElement.dataset.fjordDensity
  }, [theme, family, density])

  useEffect(() => {
    const onMode = (e: Event) => _setTheme((e as CustomEvent<ThemeMode>).detail)
    const onFamily = (e: Event) => _setFamily((e as CustomEvent<ThemeFamily>).detail)
    const onStorage = (e: StorageEvent) => {
      if (e.key === MODE_KEY || e.key === null) _setTheme(getMode())
      if (e.key === FAMILY_KEY || e.key === null) _setFamily(getFamily())
    }
    window.addEventListener(THEME_EVENT, onMode)
    window.addEventListener(FAMILY_EVENT, onFamily)
    window.addEventListener('storage', onStorage)
    loadServerTheme()
    return () => {
      window.removeEventListener(THEME_EVENT, onMode)
      window.removeEventListener(FAMILY_EVENT, onFamily)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: light)')
    if (!mq) return
    const handler = (e: MediaQueryListEvent) => {
      if (!readPreference(MODE_KEY)) _setTheme(e.matches ? 'light' : 'dark')
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  const setTheme = useCallback((value: ThemeMode) => {
    writePreference(MODE_KEY, value)
    _setTheme(value)
    window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: value }))
    if (value !== 'high-contrast') saveServerMode(value)
  }, [])
  const setFamily = useCallback((value: ThemeFamily) => {
    writePreference(FAMILY_KEY, value)
    _setFamily(value)
    window.dispatchEvent(new CustomEvent(FAMILY_EVENT, { detail: value }))
  }, [])
  const toggleTheme = useCallback(() => setTheme(theme === 'dark' ? 'light' : 'dark'), [theme, setTheme])
  const effectiveFamily = resolveFamily(theme, family)
  return { theme, setTheme, toggleTheme, family, setFamily, effectiveFamily, density, setDensity }
}
export default useTheme
