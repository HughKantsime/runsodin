// @vitest-environment node
import { afterAll, expect, it } from 'vitest'
// @ts-ignore -- Vitest provides Node; the browser tsconfig has no Node types.
import { mkdtempSync, readFileSync, readdirSync, rmSync, existsSync } from 'node:fs'
// @ts-ignore -- Vitest provides Node; the browser tsconfig has no Node types.
import { tmpdir } from 'node:os'
// @ts-ignore -- Vitest provides Node; the browser tsconfig has no Node types.
import { join, resolve } from 'node:path'
import { build } from 'vite'
import postcss from 'postcss'

const output = mkdtempSync(join(tmpdir(), 'odin-fjord-build-'))
afterAll(() => rmSync(output, { recursive: true, force: true }))
it('builds the app with local Inter and no remote font or asset URLs in any emitted CSS', async () => {
  // Disable env-file loading: this test needs no credentials or environment files.
  await build({ envDir: false, logLevel: 'silent', build: { outDir: output, emptyOutDir: true } })
  const cssFiles = readdirSync(join(output, 'assets')).filter(file => file.endsWith('.css'))
  expect(cssFiles.length).toBeGreaterThan(0)
  let interFaces = 0
  let fjordBlur = false, webkitBlur = false, blurValue = false, solidFallback = false, reducedFallback = false
  const checkUrl = (raw: string) => {
    const url = raw.trim().replace(/^['"]|['"]$/g, '')
    if (url.startsWith('data:')) return
    expect(url, 'external URL').not.toMatch(/^(?:[a-z][a-z\d+.-]*:|\/\/)/i)
    const local = url.startsWith('/') ? join(output, url.slice(1)) : resolve(output, 'assets', url)
    expect(existsSync(local), `bundled asset: ${url}`).toBe(true)
  }
  for (const file of cssFiles) {
    const css = postcss.parse(readFileSync(join(output, 'assets', file), 'utf8'))
    css.walkRules(rule => {
      if (!rule.selector.includes('fjord') || !rule.selector.includes('.sidebar')) return
      rule.walkDecls('-webkit-backdrop-filter', decl => {
        if (decl.value === 'var(--fjord-blur)') webkitBlur = true
      })
      rule.walkDecls('backdrop-filter', decl => {
        if (decl.value === 'var(--fjord-blur)' || decl.value.includes('blur(')) fjordBlur = true
        if (decl.value === 'none') reducedFallback = true
      })
      rule.walkDecls('background', decl => {
        if (decl.value === 'var(--fjord-glass-solid)') solidFallback = true
      })
    })
    css.walkAtRules('font-face', rule => {
      if (rule.nodes.some(n => n.type === 'decl' && n.prop === 'font-family' && n.value.replace(/['"]/g, '') === 'Inter')) interFaces++
    })
    css.walkAtRules('import', rule => { throw new Error(`Unbundled CSS import: ${rule.params}`) })
    css.walkDecls('--fjord-blur', decl => { if (decl.value.includes('blur(')) blurValue = true })
    css.walkDecls(decl => {
      for (const match of decl.value.matchAll(/url\(\s*([^)]*?)\s*\)/g)) checkUrl(match[1])
    })
  }
  expect(interFaces).toBeGreaterThanOrEqual(4)
  expect(fjordBlur, 'unprefixed blur survives production optimization').toBe(true)
  expect(webkitBlur, 'prefixed blur survives production optimization').toBe(true)
  expect(blurValue, 'blur variable resolves to a blur function').toBe(true)
  expect(solidFallback, 'unsupported browsers retain solid fill').toBe(true)
  expect(reducedFallback, 'reduced transparency disables unprefixed blur').toBe(true)
}, 120000)
