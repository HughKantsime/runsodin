import { describe, expect, it } from 'vitest'
import { formatGrams } from './shared'

describe('formatGrams', () => {
  it('keeps a decimal for small prints and whole grams otherwise', () => {
    expect(formatGrams(0.3)).toBe('0.3g')
    expect(formatGrams(0.55)).toBe('0.6g')
    expect(formatGrams(9.96)).toBe('10g')
    expect(formatGrams(12.4)).toBe('12g')
    expect(formatGrams(null)).toBe('—')
  })
})
