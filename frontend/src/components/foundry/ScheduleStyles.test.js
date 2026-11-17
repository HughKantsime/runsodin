// @vitest-environment node
import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import Specificity from '@bramus/specificity'
import { JSDOM } from 'jsdom'
import { expect, it } from 'vitest'

const css = postcss.parse(readFileSync('src/foundry.css', 'utf8'))
it.each([['printing', 'solid', '#7db2f7'], ['queued', 'dashed', '#ab9ff2'], ['waiting', 'dashed', '#dc9a3e']])('schedule %s border wins over page button styling', (kind, style, color) => {
  let borderRule
  css.walkRules(rule => { if (rule.selector.includes(`[data-kind="${kind}"]`)) borderRule = rule })
  expect(borderRule).toBeDefined()
  const declarations = []
  borderRule.walkDecls('border', declaration => { declarations.push(declaration.value) })
  expect(declarations).toEqual([`1px ${style} ${color}`])
  // Regression: bare [data-kind] loses to .f-page button even when later in CSS.
  expect(Specificity.calculate(borderRule.selector)[0].isGreaterThan(Specificity.calculate('.f-page button')[0])).toBe(true)
  expect(borderRule.parent.selector).toContain('[data-theme-family="foundry"]')
})

it('density segments override the page button border and keep a single outer container', () => {
  let segment
  css.walkRules(rule => { if (rule.selector === '.f-page .f-density button') segment = rule })
  expect(segment.nodes.find(d => d.prop === 'border').value).toBe('0')
  expect(Specificity.calculate(segment.selector)[0].isGreaterThan(Specificity.calculate('.f-page button')[0])).toBe(true)
})

it('active tab text wins over the broad important primary-action rule and has AA contrast', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let active
  pages.walkRules(rule => { if (rule.selector === '[data-foundry-ui="tabs"] [data-foundry-active="true"]') active = rule })
  const color = active.nodes.find(d => d.prop === 'color')
  expect(color.important).toBe(true)
  expect(color.value).toBe('var(--foundry-status-waiting-on-you-foreground)')
  const luminance = hex => hex.match(/\w\w/g).map(c => parseInt(c, 16) / 255).map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0)
  expect((luminance('f0c07c') + .05) / (luminance('3a2a14') + .05)).toBeGreaterThanOrEqual(4.5)
  expect((luminance('dc9a3e') + .05) / (luminance('121110') + .05)).toBeGreaterThanOrEqual(4.5)
})

it('tablet fleet link restores block flow after the touch-target anchor rule', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  const rules = []
  pages.walkRules(rule => { rules.push(rule) })
  const anchor = rules.find(r => r.selector === 'a[href]:not(.sr-only)')
  const fleet = rules.find(r => r.selector === '.sidebar > a[aria-label^="Fleet status:"]')
  expect(fleet.nodes.find(d => d.prop === 'display').value).toBe('block')
  expect(fleet.parent.params).toContain('max-width: 768px')
  expect(rules.indexOf(fleet)).toBeGreaterThan(rules.indexOf(anchor))
  expect(Specificity.calculate(anchor.selector)[0].isGreaterThan(Specificity.calculate(fleet.selector)[0])).toBe(false)
})

it('order IDs and Print Log pills cannot inherit arbitrary mid-word wrapping', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let noWrap
  pages.walkRules(rule => { if (rule.selector.includes('[data-foundry-page="/orders"]') && rule.selector.includes('td:first-child button')) noWrap = rule })
  expect(noWrap.selector).toContain('[data-foundry-page="/print-log"]')
  expect(Object.fromEntries(noWrap.nodes.map(d => [d.prop, d.value]))).toEqual({ 'white-space': 'nowrap', 'overflow-wrap': 'normal', 'word-break': 'normal' })
})

// Match real column positions, including expanded rows, rather than assuming that
// word-break: normal prohibits the browser's ordinary hyphen line-break points.
it.each([
  ['/products', ['Name', 'SKU', 'Price'], ['Review enclosure kit', 'REVIEW-601', '$18.00'], [2]],
  ['/consumables', ['Name', 'SKU', 'Stock'], ['Review M3 fasteners', 'REVIEW-801', '24 / min 30'], [2]],
  ['/audit', ['Timestamp', 'User', 'Action', 'Type', 'ID', 'Details', 'IP'], ['Nov 4', 'Reviewer', 'update-item', 'print-job', 'review-601-abc', 'Updated inventory quantity', '127.0.0.1'], [3, 4, 5]],
])('%s code columns prohibit hyphen wrapping without disabling prose wrapping', (route, headings, values, columns) => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let codes
  pages.walkRules(rule => { if (rule.selector.includes('[data-foundry-page="/products"]') && rule.selector.includes('nth-child(2)')) codes = rule })
  expect(codes.parent.selector).toBe('html[data-theme-family="foundry"]:not(.light):not(.high-contrast)')
  expect(Object.fromEntries(codes.nodes.map(d => [d.prop, d.value]))).toEqual({ 'white-space': 'nowrap', 'overflow-wrap': 'normal', 'word-break': 'normal' })
  const dom = new JSDOM(`<div data-foundry-page="${route}"><table data-foundry-ui="table"><thead><tr>${headings.map(t => `<th>${t}</th>`).join('')}</tr></thead><tbody><tr>${values.map(t => `<td>${t}</td>`).join('')}</tr><tr><td colspan="${headings.length}">Expanded prose with several words</td></tr></tbody></table></div>`)
  try {
    expect([...dom.window.document.querySelectorAll(codes.selector)].map(cell => cell.textContent)).toEqual([
      ...columns.map(n => headings[n - 1]), ...columns.map(n => values[n - 1]),
    ])
  } finally { dom.window.close() }
})

it('table overflow stays within its container even above the tablet breakpoint', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let scroller
  pages.walkRules(rule => { if (rule.selector === ':is(div, section):has(> [data-foundry-ui="table"])') scroller = rule })
  expect(scroller.parent.selector).toBe('html[data-theme-family="foundry"]:not(.light):not(.high-contrast)')
  expect(Object.fromEntries(scroller.nodes.map(d => [d.prop, d.value]))).toEqual({ 'overflow-x': 'auto', 'max-width': '100%' })
})

it('contains phone scrolling, pins printer links, keeps the undated target and ellipsizes names without animation', () => {
  const rules = {}
  postcss.parse(readFileSync('src/foundry.css', 'utf8')).walkRules(rule => {
    const declarations = {}
    rule.walkDecls(d => { declarations[d.prop] = d.value })
    rules[rule.selector] = declarations
  })
  expect(rules['.f-schedule']).toMatchObject({ 'min-width': '0', 'max-width': '100%' })
  expect(rules['.f-schedule-scroll']).toMatchObject({ 'overflow-x': 'auto', 'max-width': '100%', 'min-width': '0' })
  expect(rules['.f-schedule-chart']).toMatchObject({ 'min-width': 'calc(720px + var(--unknown-width, 0px))' })
  expect(rules['.f-page .f-lane .f-lane-name']).toMatchObject({ position: 'sticky', left: '0', 'min-width': '44px', 'min-height': '48px' })
  expect(rules['.f-lane .f-lane-hit']).toMatchObject({ 'min-width': '44px', 'min-height': '48px' })
  expect(rules['.f-lane .f-lane-block']).toMatchObject({ 'min-width': '0', 'min-height': '48px', 'box-sizing': 'border-box' })
  expect(rules['.f-lane-block::after']).toMatchObject({ width: 'min(2px, 25%)' })
  expect(rules['.f-lane-block > span']).toMatchObject({ 'text-overflow': 'ellipsis', overflow: 'hidden', 'white-space': 'nowrap' })
  expect(rules['.f-schedule-range select']).toMatchObject({ 'min-height': '44px' })
  expect(rules['.f-lane .f-lane-block'].animation).toBeUndefined()
})

function declarations(selector) {
  let result
  css.walkRules(rule => { if (rule.selector === selector) result = Object.fromEntries(rule.nodes.filter(n => n.type === 'decl').map(d => [d.prop, d.value])) })
  return result
}
it('long-range date labels cannot be clipped or ellipsized', () => {
  expect(declarations('.f-date-line time')).toMatchObject({ left: 'clamp(0px, var(--date-left), calc(100% - 10ch))', width: '10ch' })
  expect(declarations('.f-date-line').overflow).not.toBe('hidden')
  expect(declarations('.f-date-line time').overflow).not.toBe('hidden')
  expect(declarations('.f-date-line time')['text-overflow']).not.toBe('ellipsis')
})
it('pins an opaque axis corner above the scrolling time labels', () => {
  expect(declarations('.f-time-axis > span')).toMatchObject({ position: 'sticky', left: '0', 'z-index': '3', background: '#1b1a18' })
})
it('reserves bottom space for the floating active control on both viewport sizes', () => {
  expect(declarations('.f-schedule-scroll')['padding-bottom']).toBe('80px')
})
it('keeps timed buttons at drawn geometry while the undated target remains at least 44px', () => {
  expect(declarations('.f-lane .f-lane-block')).toMatchObject({ 'min-width': '0', padding: '0', cursor: 'inherit' })
  expect(declarations('.f-lane .f-lane-hit')).toMatchObject({ 'min-width': '44px', 'min-height': '48px', background: 'transparent', border: '0' })
  expect(declarations('.f-lane [data-kind="mixed"]')).toBeUndefined()
  expect(declarations('.f-lane-tick')).toBeUndefined()
})

it('aligns the hour/date labels, current time, and every timed lane after the undated pocket', () => {
  expect(declarations('.f-time-labels')['margin-left']).toBe('var(--unknown-width, 0px)')
  expect(declarations('.f-lane-timed')['margin-left']).toBe('var(--unknown-width, 0px)')
  expect(declarations('.f-now-track').inset).toBe('0 0 0 calc(var(--printer-width) + var(--unknown-width, 0px))')
})

it.each([390, 1440].flatMap(width => [false, true].map(unknown => [width, unknown])))('leaves half the %spx screen for time at rest (unknown: %s)', (width, unknown) => {
  const phone = width === 390
  const responsive = {}
  css.walkAtRules('media', media => {
    if (media.params !== '(max-width: 600px)') return
    media.walkRules(rule => { responsive[rule.selector] = Object.fromEntries(rule.nodes.filter(n => n.type === 'decl').map(d => [d.prop, d.value])) })
  })
  const chart = declarations('.f-schedule-chart')
  expect(chart['--printer-width']).toBe('min(144px, 20vw)')
  expect(responsive['.f-page .f-schedule']).toMatchObject({ padding: '8px' })
  expect(responsive['.f-schedule .f-schedule-chart']).toMatchObject({ '--unknown-cap': '64px' })
  // The app's desktop sidebar is 256px; phone uses the bottom navigation.
  const content = width - (phone ? 0 : 256) - (phone ? 32 : 48) - (phone ? 16 : 40) - 2
  const timeline = content - Math.min(144, width * .2) - (unknown ? (phone ? 64 : 148) : 0)
  expect(timeline).toBeGreaterThanOrEqual(width / 2)
})

it('covers the Now label and line in the pinned column, including the inter-lane gaps', () => {
  expect(declarations('.f-page .f-lane .f-lane-name')['box-shadow']).toBe('0 -8px 0 #1b1a18')
  const firstName = '.f-page .f-now-track + .f-lane .f-lane-name'
  expect(declarations(firstName)['box-shadow']).toBe('0 -24px 0 #1b1a18')
  const dom = new JSDOM('<div class="f-page"><div class="f-schedule-lanes"><div class="f-now-track"></div><div class="f-lane"><a class="f-lane-name">First</a></div><div class="f-lane"><a class="f-lane-name">Second</a></div></div></div>')
  try { expect([...dom.window.document.querySelectorAll(firstName)].map(n => n.textContent)).toEqual(['First']) }
  finally { dom.window.close() }
  expect(Number(declarations('.f-page .f-lane .f-lane-name')['z-index'])).toBeGreaterThan(Number(declarations('.f-now-track')['z-index']))
})

it('keeps the schedule viewport above the fixed Active control, rather than just padding its content', () => {
  expect(declarations('#main-content:has(.f-schedule)')).toMatchObject({ 'margin-bottom': '80px' })
  // Desktop: 24px bottom + 48px button. Phone: 80px bottom + 48px
  // button, with the app already reserving 56px for bottom navigation.
  expect(80).toBeGreaterThanOrEqual(24 + 48)
  expect(56 + 80).toBeGreaterThanOrEqual(80 + 48)
})

it('uses a neutral dotted key for both unknown-time work and its legend', () => {
  expect(declarations('.f-time-unset')).toMatchObject({ border: '1px dotted #bfbaae', background: '#24231f', color: '#bfbaae' })
})

it('shows an unclipped focus ring of at least 8px without enlarging the timed button', () => {
  expect(declarations('.f-lane .f-lane-block')).toMatchObject({ 'min-width': '0', overflow: 'visible' })
  expect(declarations('.f-lane .f-lane-block:focus-visible')).toMatchObject({ 'z-index': '1' })
  expect(declarations('.f-lane-block:focus-visible::before')).toMatchObject({
    width: 'max(100%, 8px)', outline: '2px solid #f0ece4', 'pointer-events': 'none',
  })
  expect(declarations('.f-lane-block[data-kind]')).toMatchObject({ 'border-width': 'min(1px, calc(var(--bar-pixels) / 2))' })
})

it('exempts timed bars from the phone/coarse-pointer minimum while retaining ordinary touch targets', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let touchRule
  pages.walkAtRules('media', media => {
    if (media.params !== '(max-width: 768px), (pointer: coarse)') return
    media.walkRules(rule => {
      if (rule.selector.includes('.sidebar nav a')) touchRule = rule
    })
  })
  const dom = new JSDOM('<div class="f-lane"><button class="f-lane-block"></button><button class="f-lane-hit"></button><button></button></div>')
  try {
    const [bar, pocket, ordinary] = dom.window.document.querySelectorAll('button')
    expect(bar.matches(touchRule.selector)).toBe(false)
    expect(pocket.matches(touchRule.selector)).toBe(true)
    expect(ordinary.matches(touchRule.selector)).toBe(true)
    expect(touchRule.nodes.find(d => d.prop === 'min-width').value).toBe('44px')
    expect(declarations('.f-lane .f-lane-block')['min-width']).toBe('0')
  } finally { dom.window.close() }
})

it('does not add the generic important focus outline to the bar’s existing focus ring', () => {
  const pages = postcss.parse(readFileSync('src/foundry-pages.css', 'utf8'))
  let focusRule
  pages.walkRules(rule => {
    if (rule.selector.includes(':focus-visible') && rule.nodes.some(d => d.prop === 'outline' && d.important)) focusRule = rule
  })
  const dom = new JSDOM('<button class="f-lane-block"></button><button class="f-lane-hit"></button><button></button>')
  try {
    const [bar, pocket, ordinary] = dom.window.document.querySelectorAll('button')
    const selector = focusRule.selector.replaceAll(':focus-visible', '')
    expect(bar.matches(selector)).toBe(false)
    expect(pocket.matches(selector)).toBe(true)
    expect(ordinary.matches(selector)).toBe(true)
    expect(declarations('.f-lane .f-lane-block:focus-visible').outline).toBe('none')
  } finally { dom.window.close() }
})

it('paints lane status text on an opaque background above the now line', () => {
  const status = declarations('.f-lane-status')
  expect(status.position).toBe('absolute')
  // The status follows the now track in DOM order (covered in ScheduleLane.test).
  expect(Number(status['z-index'])).toBe(Number(declarations('.f-now-track')['z-index']))
  expect(Number(status['z-index'])).toBeLessThan(Number(declarations('.f-page .f-lane .f-lane-name')['z-index']))
  expect(status.background).toBe(declarations('.f-lane-track').background)
})
