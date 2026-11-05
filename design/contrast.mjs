import { foundry } from './generate-foundry.mjs'

export function contrastRatio(first, second) {
  const luminance = hex => {
    const [r, g, b] = [1, 3, 5].map(offset => {
      const value = parseInt(hex.slice(offset, offset + 2), 16) / 255
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const a = luminance(first), b = luminance(second)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}
export function foundryPairings() {
  const texts = { ...foundry.text, action: foundry.action.primary }
  for (const [name, status] of Object.entries(foundry.status)) {
    texts[name] = status.text
    texts[`${name}BadgeText`] = status.foreground
  }
  const pairs = Object.entries(texts).flatMap(([name, foreground]) =>
    ['ground', 'panel', 'raised'].map(surface => ({ name: `${name} on ${surface}`, foreground, background: foundry.surface[surface] })))
  for (const [name, status] of Object.entries(foundry.status)) pairs.push({ name: `${name} badge`, foreground: status.foreground, background: status.background })
  pairs.push({ name: 'primary action / selection', foreground: foundry.action.onPrimary, background: foundry.action.primary })
  return pairs.map(pair => ({ ...pair, ratio: contrastRatio(pair.foreground, pair.background) }))
}
