// @vitest-environment node
import { expect, it } from 'vitest'
// @ts-ignore -- Vitest provides Node; the browser tsconfig has no Node types.
import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import { contrastRatio } from '../utils/contrast'
import { DEFAULT_SURFACE_COLORS } from '../utils/brandingPalette'

const tokens = postcss.parse(readFileSync('src/fjord-tokens.css', 'utf8'))
const styles = postcss.parse(readFileSync('src/fjord.css', 'utf8'))
const foundry = postcss.parse(readFileSync('src/foundry-tokens.css', 'utf8'))
const modes = ['dark', 'light'] as const
function declarations(mode: typeof modes[number]) {
  const selector = mode === 'light' ? 'html[data-theme-family="fjord"].light:not(.high-contrast)' : 'html[data-theme-family="fjord"]:not(.high-contrast)'
  const rule = tokens.nodes.find(n => n.type === 'rule' && n.selector === selector) as postcss.Rule
  expect(rule).toBeDefined()
  return Object.fromEntries(rule.nodes.filter(n => n.type === 'decl').map(n => [n.prop, n])) as Record<string, postcss.Declaration>
}
function values(mode: typeof modes[number]) {
  const decls = declarations(mode)
  const resolve = (key: string): string => decls[key].value.replace(/var\((--[\w-]+)\)/g, (_, name) => resolve(name))
  return Object.fromEntries(Object.keys(decls).map(key => [key, resolve(key)]))
}
it.each(modes)('defines every shared Foundry token in %s', mode => {
  const expected = new Set<string>()
  foundry.walkDecls(/^--(brand|status|chart|farm)-/, d => { expected.add(d.prop) })
  expect(expected.size).toBe(50)
  for (const key of expected) expect(declarations(mode)[key], key).toBeDefined()
})
it.each(modes)('body, muted text, inputs, chart labels and navigation meet 4.5:1 in %s', mode => {
  const v = values(mode)
  for (const bg of ['--brand-content-bg', '--brand-card-bg', '--brand-surface', '--brand-input-bg']) {
    for (const fg of ['--brand-text-primary', '--brand-text-secondary', '--brand-text-muted', '--brand-input-text', '--chart-axis']) {
      expect(contrastRatio(v[fg], v[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5)
    }
  }
  for (const [fg, bg] of [['--brand-sidebar-text', '--brand-sidebar-bg'], ['--brand-sidebar-active-text', '--brand-sidebar-active-bg'], ['--brand-on-primary', '--brand-primary'], ['--fjord-on-accent', '--brand-accent'], ['--brand-selection-text', '--brand-selection-bg']]) {
    expect(contrastRatio(v[fg], v[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5)
  }
})
it.each(modes)('every status text/tint pair meets 4.5:1 in %s', mode => {
  const v = values(mode)
  for (const state of ['printing', 'completed', 'failed', 'warning', 'pending', 'scheduled', 'neutral']) {
    const fg = v[state === 'neutral' ? '--fjord-status-neutral' : `--status-${state}`]
    for (const bg of [v[`--fjord-status-${state}-bg`], v['--brand-card-bg'], v['--brand-content-bg'], v['--brand-surface']]) {
      expect(contrastRatio(fg, bg), state).toBeGreaterThanOrEqual(4.5)
    }
  }
})
it.each(modes)('control boundaries and focus rings meet 3:1 in %s', mode => {
  const v = values(mode)
  for (const fg of ['--brand-border', '--brand-input-border', '--brand-focus-ring', '--chart-axis-line']) {
    for (const bg of ['--brand-card-bg', '--brand-content-bg', '--brand-input-bg']) {
      expect(contrastRatio(v[fg], v[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(3)
    }
  }
})
it.each(modes)('leaves personal accent, operator primary and mono unpinned in %s', mode => {
  const d = declarations(mode)
  for (const key of ['--brand-accent', '--brand-primary', '--brand-sidebar-active-text', '--brand-font-mono']) expect(d[key].important).not.toBe(true)
  expect(d['--brand-font-body'].value).toBe('-apple-system, "SF Pro Text", Inter, system-ui, sans-serif')
})
it('scopes all rules to Fjord and excludes high contrast', () => {
  for (const sheet of [tokens, styles]) {
    for (const node of sheet.nodes.filter(n => n.type !== 'comment')) {
      expect(node.type).toBe('rule')
      expect((node as postcss.Rule).selector).toMatch(/^html\[data-theme-family="fjord"\].*:not\(\.high-contrast\)$/)
    }
  }
})
it('blurs only chrome/overlays with solid and reduced-transparency fallbacks', () => {
  const blur: postcss.Declaration[] = []
  styles.walkDecls('backdrop-filter', d => { blur.push(d) })
  expect(blur).toHaveLength(2)
  const support = blur.find(d => d.value === 'var(--fjord-blur)')!.parent as postcss.AtRule
  expect(support.name).toBe('supports')
  const rule = support.parent as postcss.Rule
  expect(rule.selector).toContain('.sidebar')
  expect(rule.selector).toContain('[data-rht-toaster]')
  expect(rule.selector).not.toContain('printer-card')
  expect(rule.nodes.some(n => n.type === 'decl' && n.prop === 'background' && n.value === 'var(--fjord-glass-solid)')).toBe(true)
  const reduced = blur.find(d => d.value === 'none')!.parent as postcss.AtRule
  expect(reduced.params).toBe('(prefers-reduced-transparency: reduce)')
  expect(styles.toString()).toContain('@media (prefers-reduced-motion: reduce)')
  expect(styles.toString()).toContain('&[data-fjord-density="compact"]')
})

it.each(modes)('solid count badges and neutral/brand chips meet 4.5:1 in %s', mode => {
  const v = values(mode)
  const pairs = [
    ['--fjord-on-failed', '--status-failed'],
    ['--brand-sidebar-text', '--brand-sidebar-active-bg'],
    ['--brand-text-muted', '--brand-surface'],
    ['--brand-text-secondary', '--brand-input-bg'],
    ['--fjord-status-neutral', '--fjord-status-neutral-bg'],
  ]
  for (const [fg, bg] of pairs) expect(contrastRatio(v[fg], v[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5)
})
it.each(modes)('ready is calm and distinct from warning and brand in %s', mode => {
  const v = values(mode)
  expect(v['--fjord-status-ready']).toMatch(/^#[a-f\d]{6}$/i)
  expect(v['--fjord-status-ready']).not.toBe(v['--status-warning'])
  expect(v['--fjord-status-ready']).not.toBe(v['--brand-primary'])
  for (const bg of ['--brand-card-bg', '--brand-content-bg']) expect(contrastRatio(v['--fjord-status-ready'], v[bg])).toBeGreaterThanOrEqual(4.5)
})
it('maps idle and informational elements to calm colors while retaining warning amber', () => {
  const css = styles.toString()
  expect(css).toMatch(/\.bg-yellow-500:has\(\+ \.text-yellow-400\)[^{}]+\{ background-color: var\(--fjord-status-ready\)/)
  expect(css).toMatch(/\.bg-yellow-500 \+ \.text-yellow-400[^{}]+\{ color: var\(--fjord-status-ready\)/)
  expect(css).toMatch(/\.text-amber-200[^{}]+\{ color: var\(--status-warning\)/)
  expect(css).toMatch(/bg-amber-900\/50[^{}]+\{ background-color: var\(--fjord-status-warning-bg\)/)
  expect(css).toMatch(/\.text-yellow-200[^{}]+\{ color: var\(--status-warning\)/)
  expect(css).toMatch(/bg-yellow-900\/50[^{}]+\{ background-color: var\(--fjord-status-warning-bg\)/)
  expect(css).toContain('[data-job-priority="3"], .job-held { color: var(--fjord-status-neutral); background-color: var(--fjord-status-neutral-bg); }')
  expect(css).toContain('[data-job-action="open-printer"] { color: var(--status-printing)')
})
it('clips tab fades to the control radius and rounds segmented control containers', () => {
  const css = styles.toString()
  expect(css).toContain('[data-scrolltabs] { border-radius: var(--fjord-radius-control); overflow: hidden; }')
  expect(css).toContain('[data-scrolltabs]:has(> .mb-4) { margin-bottom: 1rem; }')
  expect(css).toContain('[data-scrolltabs]:has(> .mb-6) { margin-bottom: 1.5rem; }')
  expect(css).toContain('[data-scrolltabs] > :is(.mb-4, .mb-6) { margin-bottom: 0; }')
  expect(css).toContain('.border.rounded-md.overflow-hidden:has(> button) { border-radius: var(--fjord-radius-control); }')
})

const mix = (hex: string, other: string, alpha: number) => '#' + [1, 3, 5].map(i => Math.round(parseInt(hex.slice(i, i + 2), 16) * alpha + parseInt(other.slice(i, i + 2), 16) * (1 - alpha)).toString(16).padStart(2, '0')).join('')
it.each(modes)('brand tags and selected-tab count badges meet 4.5:1 in %s', mode => {
  const v = values(mode)
  // Follow the actual tag/count fill rules, including the inherited Classic utility.
  const tagRule = styles.toString().includes('.rounded-full[class~="bg-[var(--brand-primary)]/20"]')
  const countRule = styles.toString().includes('[data-scrolltab-active="true"] > span { background-color: transparent; }')
  const tagBg = tagRule ? v['--fjord-status-neutral-bg'] : mix(v['--brand-primary'], v['--brand-card-bg'], .2)
  const tagFg = tagRule ? v['--fjord-status-neutral'] : v['--brand-primary']
  const countBg = countRule ? v['--brand-primary'] : mix('#ffffff', v['--brand-primary'], .2)
  expect.soft(contrastRatio(tagFg, tagBg), 'printer brand tag').toBeGreaterThanOrEqual(4.5)
  expect(contrastRatio(v['--brand-on-primary'], countBg), 'selected tab count').toBeGreaterThanOrEqual(4.5)
})
it.each(modes)('Dispatch success text meets 4.5:1 in Classic and Fjord %s', mode => {
  const classic = postcss.parse(readFileSync('src/design-tokens.css', 'utf8'))
  const defaults: Record<string, string> = {}
  classic.walkRules(rule => {
    if (rule.selector === ':root' || rule.selector === (mode === 'light' ? 'html.light' : 'html:not(.light):not(.high-contrast)')) {
      rule.walkDecls(d => { defaults[d.prop] = d.value })
    }
  })
  for (const v of [defaults, values(mode)]) {
    const text = mode === 'light' ? mix(v['--status-completed'], '#000000', .55) : v['--status-completed']
    expect(contrastRatio(text, v['--brand-card-bg'])).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(text, v['--brand-content-bg'])).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(text, v['--brand-surface'])).toBeGreaterThanOrEqual(4.5)
    if (v === defaults && mode === 'dark') {
      for (const bg of [DEFAULT_SURFACE_COLORS.card_bg, DEFAULT_SURFACE_COLORS.content_bg]) expect(contrastRatio(text, bg)).toBeGreaterThanOrEqual(4.5)
    }
  }
})
