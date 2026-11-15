import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

const branding = vi.hoisted(() => ({ current: { app_name: 'O.D.I.N.', logo_url: null as string | null } }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ ...branding.current, brandingResolved: true }) }))

import OdinMark, { showsOdinMark } from './OdinMark'

afterEach(cleanup)
const PUBLIC = resolve(__dirname, '../../../public')

describe('ODIN mark', () => {
  it('shows on stock branding, drawn in the primary colour', () => {
    branding.current = { app_name: 'O.D.I.N.', logo_url: null }
    render(<OdinMark />)
    const mark = screen.getByTestId('odin-mark')
    expect(mark).toHaveAttribute('aria-hidden', 'true')
    expect(mark.style.backgroundColor).toBe('var(--brand-primary)')
    expect(mark.getAttribute('style')).toContain('/odin-mark.svg')
  })

  it('never shows on a white-labelled install: its own name or its own logo', () => {
    branding.current = { app_name: 'Acme Print Shop', logo_url: null }
    render(<OdinMark />)
    expect(screen.queryByTestId('odin-mark')).toBeNull()
    cleanup()
    branding.current = { app_name: 'O.D.I.N.', logo_url: '/static/branding/logo.png' }
    render(<OdinMark />)
    expect(screen.queryByTestId('odin-mark')).toBeNull()
    expect(showsOdinMark({ app_name: 'O.D.I.N.', logo_url: '' })).toBe(true)
    expect(showsOdinMark({ app_name: null, logo_url: null })).toBe(false)
  })
})

describe('default icon files', () => {
  // The page starts with neutral tab icons; resolved stock branding selects the SVG and PNG.
  it.each(['favicon.svg', 'favicon-neutral.svg', 'favicon-32.png', 'apple-touch-icon.png', 'odin-icon-192.svg', 'odin-icon-512.svg', 'odin-mark.svg'])('%s exists', (name) => {
    expect(existsSync(resolve(PUBLIC, name))).toBe(true)
  })

  it('every icon the page and the manifest name is a file that ships', () => {
    const html = readFileSync(resolve(PUBLIC, '../index.html'), 'utf8')
    const manifest = JSON.parse(readFileSync(resolve(PUBLIC, 'manifest.json'), 'utf8'))
    const named = [...html.matchAll(/<link[^>]+rel="(?:icon|alternate icon|apple-touch-icon)"[^>]+href="\/([^"]+)"/g)].map(m => m[1])
      .concat(manifest.icons.map((icon: { src: string }) => icon.src.replace(/^\//, '')))
    expect(named.length).toBeGreaterThanOrEqual(5)
    for (const name of named) expect(existsSync(resolve(PUBLIC, name)), name).toBe(true)
  })

  it('the SVG icons are plain paths: no scripts, no external references, no fonts', () => {
    for (const name of ['favicon.svg', 'favicon-neutral.svg', 'odin-icon-192.svg', 'odin-icon-512.svg', 'odin-mark.svg']) {
      const svg = readFileSync(resolve(PUBLIC, name), 'utf8')
      // url(#id) is a reference inside the same file (the iris clip); anything else would reach outside it.
      expect(svg, name).not.toMatch(/<script|href=|<text|<image|url\((?!#)/i)
    }
  })
})
