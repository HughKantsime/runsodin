import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { fetchAPI } from '../api'

// Import translations
import en from '../i18n/en.json'
import de from '../i18n/de.json'
import ja from '../i18n/ja.json'
import es from '../i18n/es.json'

const translations = { en, de, ja, es }

const LANGUAGES = [
  { code: 'en', name: 'English', flag: '🇺🇸' },
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'ja', name: '日本語', flag: '🇯🇵' },
  { code: 'es', name: 'Español', flag: '🇪🇸' },
]

const I18nContext = createContext()

export function I18nProvider({ children }) {
  const [locale, setLocale] = useState(() => {
    return localStorage.getItem('odin-locale') || 'en'
  })

  // Load from server config on mount
  useEffect(() => {
    fetchAPI('/settings/language').then(data => {
      if (data && data.language && translations[data.language]) {
        setLocale(data.language)
        localStorage.setItem('odin-locale', data.language)
      }
    }).catch(() => {})
  }, [])

  const changeLocale = useCallback(async (newLocale) => {
    if (!translations[newLocale]) return
    setLocale(newLocale)
    localStorage.setItem('odin-locale', newLocale)
    // Save to server
    try {
      await fetchAPI('/settings/language', {
        method: 'PUT',
        body: JSON.stringify({ language: newLocale })
      })
    } catch (e) {
      console.warn('Failed to save language preference:', e)
    }
  }, [])

  const t = useCallback((key, fallback) => {
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

export function useTranslation() {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    // Fallback for components outside provider
    return {
      t: (key, fallback) => fallback || key,
      locale: 'en',
      setLocale: () => {},
      LANGUAGES: []
    }
  }
  return ctx
}

export { LANGUAGES }
export default I18nContext
