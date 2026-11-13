import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { priceItem, marginFromMarkup, markupFromMargin } from './pricingRule'

// The same table the backend rule is tested against (tests/test_contracts/test_pricing_rule.py).
const table = JSON.parse(readFileSync(resolve(__dirname, '../../../tests/fixtures/pricing_rule_cases.json'), 'utf8'))

describe('pricing rule: the page preview equals the backend', () => {
  for (const c of table.cases as Array<{ name: string; config: any; input: any; expected: Record<string, number> }>) {
    it(c.name, () => {
      const got = priceItem(c.config, c.input) as unknown as Record<string, number>
      expect(Object.keys(got).sort()).toEqual(Object.keys(c.expected).sort())
      for (const [key, expected] of Object.entries(c.expected)) expect(got[key], key).toBeCloseTo(expected, 5)
      // To the cent, which is what a customer sees.
      expect(got.price.toFixed(2)).toBe(c.expected.price.toFixed(2))
    })
  }

  it('refuses a selling fee of 100% or more', () => {
    expect(() => priceItem({ printer_lifespan: 5000, selling_fee_percent: 100 }, { material_cost: 1, print_hours: 1 })).toThrow(RangeError)
  })
})

describe('markup and target margin are one setting', () => {
  it('converts both ways', () => {
    expect(marginFromMarkup(50)).toBeCloseTo(33.3333, 3)
    expect(markupFromMargin(33.3333333)).toBeCloseTo(50, 3)
    expect(marginFromMarkup(0)).toBe(0)
    expect(markupFromMargin(0)).toBe(0)
    expect(markupFromMargin(99.9)).toBeCloseTo(99900, 0)
  })
  it('round-trips', () => {
    for (const markup of [0, 12.5, 50, 100, 250]) expect(markupFromMargin(marginFromMarkup(markup))!).toBeCloseTo(markup, 6)
  })
  it('refuses a margin of 100 or more, or below 0', () => {
    expect(markupFromMargin(100)).toBeNull()
    expect(markupFromMargin(140)).toBeNull()
    expect(markupFromMargin(-1)).toBeNull()
    expect(markupFromMargin(NaN)).toBeNull()
  })
})
