import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { inspectFoundryLayout } from './layout_checks.mjs'
const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { JSDOM } = require('jsdom')
function fixture(html, width = 390) {
  const dom = new JSDOM(`<div data-foundry-page="/">${html}</div>`, { runScripts: 'outside-only' })
  const { window } = dom
  window.Range.prototype.getClientRects = () => [{ top: 0, width: 40, height: 15 }]
  window.innerWidth = width
  Object.defineProperty(window.document.documentElement, 'scrollWidth', { value: width })
  window.HTMLElement.prototype.checkVisibility = function () { return !this.hidden }
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    const left = Number(this.dataset.left || 0), width = Number(this.dataset.width || 44), height = Number(this.dataset.height || 44)
    return { left, right: left + width, top: 0, bottom: height, width, height }
  }
  for (const el of window.document.querySelectorAll('*')) el.style.fontSize = '15px'
  const check = () => window.eval(`(${inspectFoundryLayout.toString()})()`)
  return { window, check, close: () => dom.window.close() }
}
test('finds clipped banner actions and camera chips even with no document overflow', () => {
  const f = fixture('<article style="overflow:hidden"><button data-left="360" data-width="90">Resume</button><span data-left="20" data-width="420">Unavailable</span></article>')
  const result = f.check()
  assert.equal(result.overflow, false)
  assert.equal(result.rightOverflow.length, 2)
  f.close()
})
test('allows actual content inside an in-viewport horizontal scroller', () => {
  const f = fixture('<div id="scroll" style="overflow-x:auto" data-width="350"><table data-width="920"><tbody><tr><td data-left="900">Job</td></tr></tbody></table></div>')
  const scroller = f.window.document.getElementById('scroll')
  Object.defineProperties(scroller, { clientWidth: { value: 350 }, scrollWidth: { value: 944 }, clientLeft: { value: 0 } })
  assert.equal(f.check().rightOverflow.length, 0)
  f.close()
})
test('does not exempt a scroller or its children when the scroller itself overflows', () => {
  const f = fixture('<div id="scroll" style="overflow-x:auto" data-width="450"><button data-left="400">Action</button></div>')
  Object.defineProperties(f.window.document.getElementById('scroll'), { clientWidth: { value: 450 }, scrollWidth: { value: 800 } })
  assert.equal(f.check().rightOverflow.length, 2)
  f.close()
})
test('overflow hidden is not a scroll exemption, hidden elements are ignored', () => {
  const f = fixture('<span hidden data-width="500">Hidden</span><div style="overflow-x:hidden"><span data-width="500">Clipped</span></div>')
  assert.equal(f.check().rightOverflow.length, 1)
  f.close()
})
test('skips inert, aria-hidden and visibility-hidden subtrees but still checks an open drawer', () => {
  const f = fixture(`<div inert data-left="390"><button data-left="400">Closed inert drawer</button></div>
    <div aria-hidden="true" data-left="390"><button data-left="400">Closed aria drawer</button></div>
    <div style="visibility:hidden" data-left="390"><button style="visibility:visible" data-left="400">Hidden ancestor</button></div>
    <div aria-hidden="false"><button data-left="400">Open drawer</button></div>`)
  const result = f.check()
  assert.equal(result.rightOverflow.length, 1)
  assert.equal(result.rightOverflow[0].element, 'button Open drawer')
  f.close()
})
test('18px native checkboxes pass with a 44px label but fail with a small label', () => {
  const f = fixture('<label><input type="checkbox" data-width="18" data-height="18" /></label><label data-width="20" data-height="20"><input type="checkbox" data-width="18" data-height="18" /></label>')
  assert.equal(f.check().smallTargets.length, 1)
  f.close()
})
test('checks narrow widths including 768, leaves the desktop overflow check unchanged', () => {
  for (const width of [390, 768, 1440]) {
    const f = fixture(`<span data-left="${width}" data-width="100">Overflow</span>`, width)
    assert.equal(f.check().rightOverflow.length, width <= 768 ? 1 : 0)
    f.close()
  }
})
test('flags a single-word pill split across lines, skips multi-word controls', () => {
  const f = fixture('<span class="foundry-status">Queued</span><button>Two words</button>')
  f.window.Range.prototype.getClientRects = () => [{ top: 0, width: 25, height: 15 }, { top: 15, width: 10, height: 15 }]
  assert.equal(f.check().wrappedWords.length, 1)
  f.close()
})
test('a single line and fractional duplicate range rectangles do not flag wrapping', () => {
  const f = fixture('<span class="foundry-status">Queued</span>')
  f.window.Range.prototype.getClientRects = () => [{ top: 0.1, width: 25, height: 15 }, { top: 0.2, width: 10, height: 15 }]
  assert.equal(f.check().wrappedWords.length, 0)
  f.close()
})
test('flags split headings and table codes at both phone and tablet widths', () => {
  for (const width of [390, 768, 1440]) {
    const f = fixture('<h1>Orders</h1><h2>Stock</h2><h3>Spools</h3><table><thead><tr><th>Material</th></tr></thead><tbody><tr><td>508BCA</td><td>2010g</td></tr></tbody></table>', width)
    f.window.Range.prototype.getClientRects = () => [{ top: 0, width: 25, height: 15 }, { top: 15, width: 10, height: 15 }]
    assert.deepEqual(Array.from(f.check().wrappedWords), width <= 768
      ? ['h1 Orders', 'h2 Stock', 'h3 Spools', 'th Material', 'td 508BCA', 'td 2010g'] : [])
    f.close()
  }
})
test('word checks skip spaces, mixed markup, multiple text nodes and hidden headings or cells', () => {
  const f = fixture('<h1>Print Log</h1><h2>Two\nwords</h2><h3><span>Stock</span></h3><h1 id="split">Order</h1><h2>Stock<!-- comment --></h2><h1 hidden>Orders</h1><table><tbody><tr><td>0 rolls / 0 g</td><td><span>508BCA</span></td><td hidden>508BCA</td><td> </td></tr></tbody></table>')
  f.window.document.getElementById('split').appendChild(f.window.document.createTextNode('s'))
  f.window.Range.prototype.getClientRects = () => [{ top: 0, width: 25, height: 15 }, { top: 15, width: 10, height: 15 }]
  assert.equal(f.check().wrappedWords.length, 0)
  f.close()
})
test('single-line headings and cells do not flag wrapping', () => {
  const f = fixture('<h1>Orders</h1><h2>Stock</h2><h3>Spools</h3><table><thead><tr><th>Material</th></tr></thead><tbody><tr><td>508BCA</td></tr></tbody></table>')
  assert.equal(f.check().wrappedWords.length, 0)
  f.close()
})
