import { expect, it } from 'vitest'
import { contrastRatio, brandingContrastWarnings } from './contrast'
import { DEFAULT_SURFACE_COLORS } from './brandingPalette'

it('calculates known WCAG ratios', () => {
  expect(contrastRatio('#000000', '#ffffff')).toBe(21)
  expect(contrastRatio('#33312d', '#33312d')).toBe(1)
})
it('default branding text clears AA on card, content and input backgrounds', () => {
  for (const text of ['text_primary', 'text_secondary', 'text_muted']) {
    for (const background of ['card_bg', 'content_bg', 'input_bg']) {
      expect(contrastRatio(DEFAULT_SURFACE_COLORS[text], DEFAULT_SURFACE_COLORS[background]), `${text} on ${background}`).toBeGreaterThanOrEqual(4.5)
    }
  }
})
it('reports every failing custom text/background pair without changing it', () => {
  const palette = { ...DEFAULT_SURFACE_COLORS, text_muted: '#58554a' }
  expect(brandingContrastWarnings(palette)).toEqual(expect.arrayContaining([
    expect.objectContaining({ text: 'text_muted', background: 'card_bg' }),
    expect.objectContaining({ text: 'text_muted', background: 'input_bg' }),
  ]))
  expect(palette.text_muted).toBe('#58554a')
})
