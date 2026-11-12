import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'
import ScrollTabs from './ScrollTabs'

const scrollIntoView = vi.fn()
let reduced = false
let phone = true

function strip(active: 'a' | 'b') {
  return (
    <ScrollTabs data-testid="strip">
      <div>
        <button data-scrolltab-active={active === 'a' ? 'true' : undefined}>A</button>
        <button data-scrolltab-active={active === 'b' ? 'true' : undefined}>B</button>
      </div>
    </ScrollTabs>
  )
}

function geometry(el: HTMLElement, g: { scrollLeft: number; clientWidth: number; scrollWidth: number }) {
  for (const [k, v] of Object.entries(g)) Object.defineProperty(el, k, { configurable: true, value: v })
}

beforeEach(() => {
  reduced = false
  phone = true
  scrollIntoView.mockClear()
  Element.prototype.scrollIntoView = scrollIntoView
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q.includes('prefers-reduced-motion') ? reduced : q.includes('min-width: 640px') ? phone : false,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ScrollTabs active tab', () => {
  it('scrolls the active tab into view on mount, nearest inline, no vertical jump', () => {
    const { getByText } = render(strip('b'))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(scrollIntoView).toHaveBeenCalledWith({ inline: 'nearest', block: 'nearest', behavior: 'auto' })
    expect(scrollIntoView.mock.contexts[0]).toBe(getByText('B'))
  })

  it('scrolls the new tab into view when the active tab changes, smoothly by default', () => {
    const { rerender, getByText } = render(strip('a'))
    scrollIntoView.mockClear()
    rerender(strip('b'))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(scrollIntoView).toHaveBeenCalledWith({ inline: 'nearest', block: 'nearest', behavior: 'smooth' })
    expect(scrollIntoView.mock.contexts[0]).toBe(getByText('B'))
  })

  it('does not scroll again when a re-render keeps the same active tab', () => {
    const { rerender } = render(strip('a'))
    scrollIntoView.mockClear()
    rerender(strip('a'))
    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('does not use smooth scrolling under prefers-reduced-motion', () => {
    reduced = true
    const { rerender } = render(strip('a'))
    scrollIntoView.mockClear()
    rerender(strip('b'))
    expect(scrollIntoView).toHaveBeenCalledWith({ inline: 'nearest', block: 'nearest', behavior: 'auto' })
  })

  it('never scrolls on its own at 640 px and above, and its fades are hidden there', () => {
    phone = false
    const { container } = render(strip('b'))
    expect(scrollIntoView).not.toHaveBeenCalled()
    for (const fade of container.querySelectorAll('[data-scrolltabs-fade]')) expect(fade.className).toContain('sm:hidden')
  })

  it('also recognises aria-pressed (Foundry TabBar)', () => {
    const { getByText } = render(<ScrollTabs><div><button>A</button><button aria-pressed="true">B</button></div></ScrollTabs>)
    expect(scrollIntoView.mock.contexts[0]).toBe(getByText('B'))
  })
})

describe('ScrollTabs edge fades', () => {
  const left = (c: HTMLElement) => c.querySelector('[data-scrolltabs-fade="left"]')
  const right = (c: HTMLElement) => c.querySelector('[data-scrolltabs-fade="right"]')

  it('shows no fades when everything fits', () => {
    const { container, getByTestId } = render(strip('a'))
    geometry(getByTestId('strip'), { scrollLeft: 0, clientWidth: 300, scrollWidth: 300 })
    fireEvent.scroll(getByTestId('strip'))
    expect(left(container)).toBeNull()
    expect(right(container)).toBeNull()
  })

  it('shows only the right fade at the start of an overflowing strip', () => {
    const { container, getByTestId } = render(strip('a'))
    geometry(getByTestId('strip'), { scrollLeft: 0, clientWidth: 200, scrollWidth: 500 })
    fireEvent.scroll(getByTestId('strip'))
    expect(left(container)).toBeNull()
    expect(right(container)).not.toBeNull()
  })

  it('shows both fades mid-scroll and only the left fade at the end', () => {
    const { container, getByTestId } = render(strip('a'))
    const el = getByTestId('strip')
    geometry(el, { scrollLeft: 100, clientWidth: 200, scrollWidth: 500 })
    fireEvent.scroll(el)
    expect(left(container)).not.toBeNull()
    expect(right(container)).not.toBeNull()
    geometry(el, { scrollLeft: 300, clientWidth: 200, scrollWidth: 500 })
    fireEvent.scroll(el)
    expect(left(container)).not.toBeNull()
    expect(right(container)).toBeNull()
  })

  it('fades are hidden from assistive tech and never intercept pointer events', () => {
    const { container, getByTestId } = render(strip('a'))
    geometry(getByTestId('strip'), { scrollLeft: 0, clientWidth: 200, scrollWidth: 500 })
    fireEvent.scroll(getByTestId('strip'))
    const fade = right(container) as HTMLElement
    expect(fade).toHaveAttribute('aria-hidden', 'true')
    expect(fade.className).toContain('pointer-events-none')
  })
})
