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
  // Only leaf text in short controls: no icons, mixed fonts, multiline prose or
  // hidden measurement spans. Range rectangles report real rendered text lines.
  const wrappedWords = innerWidth <= 768 ? elements.filter(el => visible(el) && !el.children.length
    && el.matches('button, .foundry-status, [role="status"], span[class*="rounded"]')
    && /^\S+$/.test(el.textContent.trim())).filter(el => {
      const range = document.createRange()
      range.selectNodeContents(el)
      const lines = new Set([...range.getClientRects()].filter(r => r.width > 0 && r.height > 0).map(r => Math.round(r.top)))
      return lines.size > 1
    }).map(describe) : []
  return { smallText, smallTargets, overflow: document.documentElement.scrollWidth > innerWidth, rightOverflow, wrappedWords }
}
