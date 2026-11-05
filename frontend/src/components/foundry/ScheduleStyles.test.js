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
