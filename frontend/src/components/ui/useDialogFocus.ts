import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Focus management for a modal dialog (WCAG 2.4.3): while `active`, focus moves
 * into the dialog, Tab and Shift+Tab stay inside it, Escape calls `onEscape`
 * (when given), and on deactivation focus returns to the element that had it.
 */
export function useDialogFocus(ref: RefObject<HTMLElement>, active: boolean, onEscape?: () => void) {
  const escapeRef = useRef(onEscape)
  useEffect(() => { escapeRef.current = onEscape }, [onEscape])

  useEffect(() => {
    if (!active) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = ref.current
    const first = root?.querySelector<HTMLElement>(FOCUSABLE)
    if (first) first.focus()
    else if (root) {
      if (!root.hasAttribute('tabindex')) root.setAttribute('tabindex', '-1')
      root.focus()
    }

    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && escapeRef.current) {
        escapeRef.current()
        return
      }
      if (e.key !== 'Tab' || !ref.current) return
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const firstItem = items[0]
      const lastItem = items[items.length - 1]
      const inside = ref.current.contains(document.activeElement)
      if (e.shiftKey && (document.activeElement === firstItem || !inside)) {
        e.preventDefault()
        lastItem.focus()
      } else if (!e.shiftKey && (document.activeElement === lastItem || !inside)) {
        e.preventDefault()
        firstItem.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('keydown', handleKey)
      if (previous && document.contains(previous)) previous.focus()
    }
  }, [active, ref])
}
