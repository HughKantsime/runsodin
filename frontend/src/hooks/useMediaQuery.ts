import { useEffect, useState } from 'react'

function supported(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
}

/** Tolerates jsdom (no matchMedia) and partial test stubs: falls back to `false`. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => (supported() ? window.matchMedia(query)?.matches === true : false))
  useEffect(() => {
    if (!supported()) return
    const mql = window.matchMedia(query)
    if (!mql) return
    const update = () => setMatches(mql.matches === true)
    update()
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', update)
      return () => mql.removeEventListener?.('change', update)
    }
    if (typeof mql.addListener === 'function') {
      mql.addListener(update)
      return () => mql.removeListener?.(update)
    }
  }, [query])
  return matches
}
