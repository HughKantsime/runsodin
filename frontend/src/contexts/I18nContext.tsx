import { createContext, useContext, useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { fetchAPI } from '../api'

// Import translations
import en from '../i18n/en.json'
import de from '../i18n/de.json'
import ja from '../i18n/ja.json'
import es from '../i18n/es.json'
import it from '../i18n/it.json'

type Locale = 'en' | 'de' | 'ja' | 'es' | 'it'
const translations: Record<Locale, Record<string, string>> = { en, de, ja, es, it }
const isSupportedLocale = (value: unknown): value is Locale => typeof value === 'string' && Object.prototype.hasOwnProperty.call(translations, value)

const LANGUAGES = [
  { code: 'en', name: 'English', flag: '🇺🇸' },
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'it', name: 'Italiano', flag: '🇮🇹' },
  { code: 'ja', name: '日本語', flag: '🇯🇵' },
  { code: 'es', name: 'Español', flag: '🇪🇸' },
]

interface I18nValue {
  locale: Locale;
  setLocale: (locale: string) => Promise<void>;
  t: (key: string, fallback?: string) => string;
  LANGUAGES: typeof LANGUAGES;
}
const I18nContext = createContext<I18nValue | null>(null)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(() => {
    const saved = localStorage.getItem('odin-locale')
    return isSupportedLocale(saved) ? saved : 'en'
  })

  const choiceRevision = useRef(0)
  const mounted = useRef(false)
  // Load from server config on mount
  useEffect(() => {
    mounted.current = true
    let active = true
    const revision = choiceRevision.current
    fetchAPI<{ language: unknown }>('/settings/language').then(data => {
      if (active && revision === choiceRevision.current && data && isSupportedLocale(data.language)) {
        setLocale(data.language)
        localStorage.setItem('odin-locale', data.language)
      }
    }).catch(() => {})
    return () => { active = false; mounted.current = false }
  }, [])

  const changeLocale = useCallback(async (newLocale: string) => {
    if (!isSupportedLocale(newLocale)) return
    const revision = ++choiceRevision.current
    const previous = localStorage.getItem('odin-locale') || 'en'
    setLocale(newLocale)
    localStorage.setItem('odin-locale', newLocale)
    // Save to server
    try {
      await fetchAPI('/settings/language', {
        method: 'PUT',
        body: JSON.stringify({ language: newLocale })
      })
    } catch (e) {
      if (mounted.current && choiceRevision.current === revision) {
        const restored = isSupportedLocale(previous) ? previous : 'en'
        setLocale(restored)
        localStorage.setItem('odin-locale', restored)
      }
      throw e
    }
  }, [])

  const t = useCallback((key: string, fallback?: string) => {
    const current = translations[locale]
    if (current && current[key]) return current[key]
    // Fallback to English
    if (translations.en[key]) return translations.en[key]
    // Fallback to provided default or key itself
    return fallback || key
  }, [locale])

  return (
    <I18nContext.Provider value={{ locale, setLocale: changeLocale, t, LANGUAGES }}>
      {children}
    </I18nContext.Provider>
  )
}

export function useTranslation(): I18nValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    // Fallback for components outside provider
    return {
      t: (key, fallback) => translations.en[key] || fallback || key,
      locale: 'en',
      setLocale: async () => {},
      LANGUAGES
    }
  }
  return ctx
}

export { LANGUAGES }
export default I18nContext
