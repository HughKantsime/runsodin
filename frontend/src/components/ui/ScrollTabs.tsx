import { useCallback, useEffect, useRef, useState, type HTMLAttributes, type ReactNode } from 'react'
import clsx from 'clsx'

// Marks the selected tab. TabBar sets data-scrolltab-active in every theme; the
// aria selectors cover Foundry and any hand-rolled strip that uses ARIA.
const ACTIVE_TAB = '[data-scrolltab-active="true"],[aria-selected="true"],[aria-pressed="true"],[aria-current="true"],[data-foundry-active="true"]'

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)')?.matches === true
}

function isPhoneWidth(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true
  return window.matchMedia('not all and (min-width: 640px)')?.matches !== false
}

interface ScrollTabsProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
  /** Classes for the outer (positioning) wrapper. `className` stays on the scroll container. */
  wrapperClassName?: string
  /** Gradient start colour class for the edge fades; match the strip background. */
  fadeClassName?: string
}

/**
 * Horizontally scrolling tab strip behaviour: the active tab is scrolled into view
 * on mount and whenever it changes, and edge fades show only while there is more
 * to scroll. All of it (auto-scroll, fades, nowrap/shrink rules) applies below `sm`
 * only, so >= 640 px layout and behaviour are exactly what the wrapped content already did.
 */
export default function ScrollTabs({
  children,
  className,
  wrapperClassName,
  fadeClassName = 'from-[var(--brand-content-bg)]',
  ...rest
}: ScrollTabsProps) {
  const scroller = useRef<HTMLDivElement>(null)
  const lastActive = useRef<Element | null>(null)
  const [fade, setFade] = useState({ left: false, right: false })

  const measure = useCallback(() => {
    const el = scroller.current
    if (!el) return
    const left = el.scrollLeft > 1
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1
    setFade(prev => (prev.left === left && prev.right === right ? prev : { left, right }))
  }, [])

  // When the strip or its content changes size (fonts and icons arriving after the first paint
  // widen the tabs), the tab scrolled into view on mount can end up cut off again: put it back.
  const keepActiveVisible = useCallback(() => {
    const el = scroller.current
    const active = el?.querySelector(ACTIVE_TAB)
    if (!el || !active || !isPhoneWidth()) return
    const strip = el.getBoundingClientRect()
    const tab = active.getBoundingClientRect()
    // Move the strip only (never the page), and only as far as needed.
    if (tab.left < strip.left - 1) el.scrollLeft -= strip.left - tab.left
    else if (tab.right > strip.right + 1) el.scrollLeft += tab.right - strip.right
  }, [])

  // After every render: bring a newly active tab into view, then re-measure.
  useEffect(() => {
    const active = scroller.current?.querySelector(ACTIVE_TAB) ?? null
    // Phone widths only (the same breakpoint as the max-sm rules below): at 640 px and
    // above nothing scrolls on its own and no fade is drawn, so desktop is unchanged.
    if (active && active !== lastActive.current && isPhoneWidth()) {
      const firstTime = lastActive.current === null
      lastActive.current = active
      active.scrollIntoView?.({
        inline: 'nearest',
        block: 'nearest',
        behavior: firstTime || prefersReducedMotion() ? 'auto' : 'smooth',
      })
    }
    measure()
  })

  useEffect(() => {
    const el = scroller.current
    if (!el) return
    el.addEventListener('scroll', measure, { passive: true })
    window.addEventListener('resize', measure)
    let observer: ResizeObserver | undefined
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => { keepActiveVisible(); measure() })
      observer.observe(el)
      if (el.firstElementChild) observer.observe(el.firstElementChild)
    }
    return () => {
      el.removeEventListener('scroll', measure)
      window.removeEventListener('resize', measure)
      observer?.disconnect()
    }
  }, [measure, keepActiveVisible])

  const fadeBase = clsx('pointer-events-none absolute inset-y-0 w-8 to-transparent sm:hidden', fadeClassName)

  return (
    <div className={clsx('relative', wrapperClassName)}>
      <div
        ref={scroller}
        className={clsx(
          className,
          'max-sm:[&_button]:shrink-0 max-sm:[&_button]:whitespace-nowrap max-sm:[&>div]:w-max max-sm:[&>div]:min-w-full',
        )}
        {...rest}
      >
        {children}
      </div>
      {fade.left && <span aria-hidden="true" data-scrolltabs-fade="left" className={clsx(fadeBase, 'left-0 bg-gradient-to-r')} />}
      {fade.right && <span aria-hidden="true" data-scrolltabs-fade="right" className={clsx(fadeBase, 'right-0 bg-gradient-to-l')} />}
    </div>
  )
}
