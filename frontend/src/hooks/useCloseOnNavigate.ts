import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'

// Runs `close` on every navigation (back/forward, search result, programmatic, same-path query or hash change).
// location.key changes on each history entry; pathname alone misses same-page navigation.
export function useCloseOnNavigate(close: () => void) {
  const { key } = useLocation()
  const closeRef = useRef(close)
  useEffect(() => {
    closeRef.current = close
  }, [close])
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }   // only a CHANGE of location closes
    closeRef.current()
  }, [key])
}
