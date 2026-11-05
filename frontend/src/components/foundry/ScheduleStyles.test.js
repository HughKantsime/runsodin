// @vitest-environment node
import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import Specificity from '@bramus/specificity'
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
