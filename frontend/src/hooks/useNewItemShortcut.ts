import { useEffect, useRef } from 'react'

/**
 * Subscribe a page to the context-dependent `N` shortcut. useKeyboardShortcuts
 * dispatches window `new-item` with `{ page }` for /spools, /orders, /products
 * and /consumables; the page opens its create dialog when `enabled` (the same
 * permission that shows its "New"/"Add" button).
 */
export function useNewItemShortcut(page: string, onNew: () => void, enabled = true) {
  const handler = useRef(onNew)
  handler.current = onNew
  useEffect(() => {
    if (!enabled) return
    const listener = (event: Event) => {
      if ((event as CustomEvent<{ page?: string }>).detail?.page === page) handler.current()
    }
    window.addEventListener('new-item', listener)
    return () => window.removeEventListener('new-item', listener)
  }, [page, enabled])
}

export default useNewItemShortcut
