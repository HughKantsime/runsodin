/** WCAG 2 relative luminance and contrast for opaque six-digit hex colours. */
export function contrastRatio(first: string, second: string): number {
  const luminance = (hex: string) => {
    if (!/^#[0-9a-f]{6}$/i.test(hex)) return NaN
    const [r, g, b] = [1, 3, 5].map(offset => {
      const channel = parseInt(hex.slice(offset, offset + 2), 16) / 255
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const a = luminance(first), b = luminance(second)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

export function brandingContrastWarnings(palette: Record<string, string>) {
  const texts = ['text_primary', 'text_secondary', 'text_muted']
  const backgrounds = ['card_bg', 'content_bg', 'input_bg']
  const pairs = texts.flatMap(text => backgrounds.map(background => [text, background]))
  pairs.push(['sidebar_text', 'sidebar_bg'], ['sidebar_active_text', 'sidebar_active_bg'])
  return pairs.flatMap(([text, background]) => {
    const ratio = contrastRatio(palette[text], palette[background])
    return ratio < 4.5 ? [{ text, background, ratio }] : []
  })
}
