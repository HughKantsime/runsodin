import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'

// Runs `close` when the user moves to another page (back/forward, a search result, programmatic navigation).
// Compares against the previous path: never fires on first render (also under StrictMode's double effect),
// and a page that only rewrites its own query string (a tab or filter) does not close anything.
export function useCloseOnNavigate(close: () => void) {
  const { pathname } = useLocation()
  const closeRef = useRef(close)
  const previous = useRef(pathname)
  useEffect(() => {
    closeRef.current = close
  }, [close])
  useEffect(() => {
    if (previous.current === pathname) return
    previous.current = pathname
    closeRef.current()
  }, [pathname])
}
