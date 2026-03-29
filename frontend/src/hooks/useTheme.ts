import { useState, useEffect, useCallback } from 'react'

const STORAGE_KEY = 'odin-theme'

function getSystemPreference() {
  if (typeof window === 'undefined') return 'dark'
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function getInitialTheme() {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return getSystemPreference()
}

/**
 * Theme hook with system color scheme detection.
 *
 * Reads from localStorage ('odin-theme'). If no stored preference exists,
 * falls back to the OS prefers-color-scheme media query.
 *
 * Returns { theme, setTheme, toggleTheme }.
 *
 * Usage:
 *   const { theme, toggleTheme } = useTheme()
 *   <button onClick={toggleTheme}>...</button>
 */
export function useTheme() {
  const [theme, _setTheme] = useState(getInitialTheme)

  // Apply theme class to <html> whenever theme changes
  useEffect(() => {
    document.documentElement.classList.toggle('light', theme === 'light')
  }, [theme])

  // Listen for OS-level color scheme changes. Only respond if the user
  // has never explicitly chosen a theme (no stored preference).
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: light)')
    const handler = (e) => {
      if (!localStorage.getItem(STORAGE_KEY)) {
        _setTheme(e.matches ? 'light' : 'dark')
      }
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  // setTheme and toggleTheme persist to localStorage, marking an
  // explicit user choice (which stops OS-level auto-switching).
  const setTheme = useCallback((value) => {
    localStorage.setItem(STORAGE_KEY, value)
    _setTheme(value)
  }, [])

  const toggleTheme = useCallback(() => {
    _setTheme(prev => {
      const next = prev === 'dark' ? 'light' : 'dark'
      localStorage.setItem(STORAGE_KEY, next)
      return next
    })
  }, [])

  return { theme, setTheme, toggleTheme }
}

export default useTheme
