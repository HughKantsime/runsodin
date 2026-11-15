import { useLayoutEffect, useRef, useState } from 'react'

/** Measure the rendered clamp again when its width, font metrics or text changes. */
export function useTextOverflow<T extends HTMLElement>(text: string) {
  const ref = useRef<T>(null)
  const [overflow, setOverflow] = useState(false)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const measure = () => setOverflow(element.scrollHeight > element.clientHeight + 1)
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(element)
    window.addEventListener('resize', measure)
    document.fonts?.addEventListener('loadingdone', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      document.fonts?.removeEventListener('loadingdone', measure)
    }
  }, [text])
  return { ref, overflow }
}
