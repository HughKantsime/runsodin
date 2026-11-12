/** Self-contained so Playwright can evaluate it directly in either worktree. */
export function inspectFoundryLayout() {
  const visible = el => {
    if (el.closest('[inert], [aria-hidden="true"]')) return false
    for (let ancestor = el; ancestor; ancestor = ancestor.parentElement) {
      if (getComputedStyle(ancestor).visibility === 'hidden') return false
    }
    const rect = el.getBoundingClientRect()
    return el.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && rect.width > 0 && rect.height > 0
  }
  const describe = el => `${el.tagName.toLowerCase()} ${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 80)}`
  const elements = [...document.querySelectorAll('[data-foundry-page] *')]
  const smallText = elements.filter(el => visible(el) && [...el.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim()) && parseFloat(getComputedStyle(el).fontSize) < 13).map(describe)
  // Native checkboxes can be 18px while their actual clickable labels are 44px.
  const targetSize = el => {
    const candidates = [el, ...Array.from(el.labels || [])].filter(visible)
    return candidates.some(target => {
      const rect = target.getBoundingClientRect()
      return rect.width >= 44 && rect.height >= 44
    })
  }
  const smallTargets = innerWidth <= 768 ? [...document.querySelectorAll('button, a[href], input:not([type="hidden"]), select, summary, [role="button"]')]
    .filter(el => visible(el) && !el.classList.contains('sr-only') && !targetSize(el)).map(describe) : []
  const insideHorizontalScroller = el => {
    const rect = el.getBoundingClientRect()
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      if (!['auto', 'scroll'].includes(getComputedStyle(parent).overflowX) || parent.scrollWidth <= parent.clientWidth) continue
      const bounds = parent.getBoundingClientRect()
      // A scroller that itself extends outside the viewport cannot excuse its children.
      const contentLeft = bounds.left + parent.clientLeft - parent.scrollLeft
      if (bounds.left >= 0 && bounds.right <= innerWidth + 1
        && rect.left >= contentLeft - 1 && rect.right <= contentLeft + parent.scrollWidth + 1) return true
    }
    return false
  }
  const rightOverflow = innerWidth <= 768 ? elements.filter(el => visible(el)
    && el.getBoundingClientRect().right > innerWidth + 1 && !insideHorizontalScroller(el))
    .map(el => ({ element: describe(el), right: el.getBoundingClientRect().right })) : []
  // A single text node in headings, cells or short controls: no icons, mixed
  // fonts or multiline prose. Range rectangles report real rendered text lines.
  const wrappedWords = innerWidth <= 768 ? elements.filter(el => visible(el)
    && el.childNodes.length === 1 && el.firstChild.nodeType === Node.TEXT_NODE
    && el.matches('h1, h2, h3, td, th, button, .foundry-status, [role="status"], span[class*="rounded"]')
    && /^\S+$/.test(el.textContent.trim())).filter(el => {
      const range = document.createRange()
      range.selectNodeContents(el)
      const lines = new Set([...range.getClientRects()].filter(r => r.width > 0 && r.height > 0).map(r => Math.round(r.top)))
      return lines.size > 1
    }).map(describe) : []
  return { smallText, smallTargets, overflow: document.documentElement.scrollWidth > innerWidth, rightOverflow, wrappedWords }
}

/** Phone-width (<= 480 px) checks. Split in two:
 *  - collectPhoneLayout runs in the page and returns plain rectangles/flags only (no judging);
 *  - the judge functions below are pure and unit tested with plain objects.
 */
export const PHONE_CHECKS = ['documentOverflow', 'clippedRight', 'overlapsBottomNav', 'activeTabHidden', 'smallTapTargets']

/** Self-contained (evaluated with Playwright's page.evaluate and, in tests, via toString). */
export function collectPhoneLayout() {
  const visible = el => {
    if (el.closest('[inert], [aria-hidden="true"]')) return false
    for (let ancestor = el; ancestor; ancestor = ancestor.parentElement) {
      if (getComputedStyle(ancestor).visibility === 'hidden') return false
    }
    const rect = el.getBoundingClientRect()
    return el.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && rect.width > 0 && rect.height > 0
  }
  const rectOf = el => {
    const r = el.getBoundingClientRect()
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }
  }
  const hint = el => {
    const id = el.id ? `#${el.id}` : ''
    const classes = (el.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 3).map(c => `.${c}`).join('')
    const text = (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40)
    return `${el.tagName.toLowerCase()}${id}${classes}${text ? ` "${text}"` : ''}`
  }
  const scrollsX = el => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX) && el.scrollWidth > el.clientWidth
  // Same rule as inspectFoundryLayout: a scroller that itself leaves the viewport excuses nothing.
  const inScroller = el => {
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      if (!scrollsX(parent)) continue
      const bounds = parent.getBoundingClientRect()
      if (bounds.left >= 0 && bounds.right <= innerWidth + 1) return true
    }
    return false
  }
  const nav = [...document.querySelectorAll('nav')].find(n => visible(n)
    && getComputedStyle(n).position === 'fixed' && n.getBoundingClientRect().bottom >= innerHeight - 1) || null
  const elements = []
  for (const el of document.body.querySelectorAll('*')) {
    const rect = rectOf(el)
    const position = getComputedStyle(el).position
    const pinned = position === 'fixed' || position === 'sticky'
    if (!pinned && rect.right <= innerWidth + 1) continue
    if (!visible(el)) continue
    elements.push({ label: hint(el), position, rect, inScroller: inScroller(el), isNav: el === nav, inNav: !!nav && nav !== el && nav.contains(el) })
  }
  const activeSelector = '[data-scrolltab-active="true"],[aria-selected="true"],[aria-pressed="true"],[aria-current]:not([aria-current="false"])'
  const strips = []
  for (const el of document.body.querySelectorAll('*')) {
    if (!scrollsX(el) || !visible(el)) continue
    const actives = [...el.querySelectorAll(activeSelector)].filter(tab => tab.matches('button, a, [role="tab"]') && visible(tab))
    if (actives.length) strips.push({ label: hint(el), rect: rectOf(el), actives: actives.map(tab => ({ label: hint(tab), rect: rectOf(tab) })) })
  }
  const targets = []
  if (nav) for (const el of nav.querySelectorAll('button, a[href]')) if (visible(el)) targets.push({ label: hint(el), area: 'nav', rect: rectOf(el) })
  // Row/card actions: controls inside a table row, list item, article, row role or a card-classed container,
  // excluding navigation chrome (nav, aside, header) and screen-reader-only controls.
  const actionContainer = 'tr, li, article, [role="row"], [data-card], [class*="card"]'
  for (const el of document.body.querySelectorAll('button, a[href], [role="button"]')) {
    if (targets.length >= 500) break
    if (el.classList.contains('sr-only') || el.closest('nav, aside, header') || !el.closest(actionContainer) || !visible(el)) continue
    targets.push({ label: hint(el), area: 'action', rect: rectOf(el) })
  }
  return {
    viewport: { width: innerWidth, height: innerHeight },
    scrollWidth: document.documentElement.scrollWidth,
    nav: nav ? rectOf(nav) : null,
    elements, strips, targets,
  }
}

export function documentOverflow(data) {
  return data.scrollWidth > data.viewport.width + 1
}

export function clippedRight(data) {
  return data.elements
    .filter(e => e.rect.right > data.viewport.width + 1 && !e.inScroller)
    .map(e => ({ element: e.label, right: e.rect.right }))
}

export function overlapsBottomNav(data) {
  if (!data.nav) return []
  const nav = data.nav
  return data.elements
    .filter(e => (e.position === 'fixed' || e.position === 'sticky') && !e.isNav && !e.inNav)
    .map(e => ({
      element: e.label,
      width: Math.min(e.rect.right, nav.right) - Math.max(e.rect.left, nav.left),
      height: Math.min(e.rect.bottom, nav.bottom) - Math.max(e.rect.top, nav.top),
    }))
    .filter(o => o.width > 1 && o.height > 1)
}

export function activeTabHidden(data) {
  const found = []
  for (const strip of data.strips) {
    for (const tab of strip.actives) {
      if (tab.rect.left < strip.rect.left - 1 || tab.rect.right > strip.rect.right + 1) found.push({ strip: strip.label, tab: tab.label })
    }
  }
  return found
}

export function smallTapTargets(data) {
  return data.targets
    .filter(t => t.rect.width < 44 || t.rect.height < 44)
    .slice(0, 20)
    .map(t => ({ element: t.label, area: t.area, width: Math.round(t.rect.width), height: Math.round(t.rect.height) }))
}

/** overlay: a dialog/drawer is open on purpose, so nav overlap and tab-strip position are not judged. */
export function judgePhoneLayout(data, { overlay = false } = {}) {
  return {
    documentOverflow: documentOverflow(data),
    clippedRight: clippedRight(data),
    overlapsBottomNav: overlay ? [] : overlapsBottomNav(data),
    activeTabHidden: overlay ? [] : activeTabHidden(data),
    smallTapTargets: smallTapTargets(data),
  }
}

const countOf = value => typeof value === 'boolean' ? Number(value) : value.length
export function phoneFindingCount(findings) {
  return PHONE_CHECKS.reduce((sum, name) => sum + countOf(findings[name]), 0)
}
export function phoneFindingSummary(findings) {
  return PHONE_CHECKS.filter(name => countOf(findings[name])).map(name => `${name}: ${countOf(findings[name])}`).join(', ')
}
/** entries: report.phoneLayout items ({ theme, ...findings }). */
export function summarizePhoneLayout(entries) {
  const byTheme = {}
  let withFindings = 0
  for (const entry of entries) {
    const totals = byTheme[entry.theme] ||= Object.fromEntries(PHONE_CHECKS.map(name => [name, 0]))
    if (phoneFindingCount(entry)) withFindings++
    for (const name of PHONE_CHECKS) totals[name] += countOf(entry[name])
  }
  return { captures: entries.length, withFindings, byTheme }
}
