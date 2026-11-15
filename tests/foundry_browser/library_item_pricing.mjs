/** Offline card layout check. Uses installed packages/browser only; no server or network.
 * Run: node tests/foundry_browser/library_item_pricing.mjs [output directory] [--build-only]
 */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdtemp, readFile, writeFile, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const root = path.resolve(import.meta.dirname, '../..')
const frontend = path.join(root, 'frontend')
const require = createRequire(path.join(frontend, 'package.json'))
const { build } = await import(pathToFileURL(require.resolve('vite')).href)
const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href)
const { chromium } = require('@playwright/test')
const cases = JSON.parse(await readFile(path.join(root, 'tests/fixtures/library_item_pricing.json'), 'utf8'))
const output = path.resolve(process.argv.slice(2).find(arg => arg !== '--build-only') || path.join(tmpdir(), 'odin-library-item-pricing'))
const scratch = await mkdtemp(path.join(tmpdir(), 'odin-library-layout-'))
let browser
try {
  await mkdir(output, { recursive: true })
  const entry = path.join(scratch, 'cards.tsx')
  await writeFile(entry, `
    import React from ${JSON.stringify(require.resolve('react'))}
    import { createRoot } from ${JSON.stringify(require.resolve('react-dom/client'))}
    import { QueryClient, QueryClientProvider } from ${JSON.stringify(require.resolve('@tanstack/react-query'))}
    import { ModelCard } from ${JSON.stringify(path.join(frontend, 'src/components/models/ModelLibraryFields.tsx'))}
    import ${JSON.stringify(path.join(frontend, 'src/index.css'))}
    const cases = ${JSON.stringify(cases)}
    const noop = () => {}
    createRoot(document.getElementById('root')).render(
      <QueryClientProvider client={new QueryClient()}>
        <main className="p-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          {cases.map(c => <section key={c.name} aria-label={c.name}>
            <ModelCard model={{...c.model, ...c.expected}} onEdit={noop} onDelete={noop}
              onSchedule={noop} onToggleFavorite={noop} onView3D={noop} onRevisions={noop} />
          </section>)}
        </main>
      </QueryClientProvider>)
  `)
  const bundle = await build({
    root: frontend, configFile: false, envDir: false, plugins: [react()], logLevel: 'warn',
    resolve: { alias: { 'react/jsx-runtime': require.resolve('react/jsx-runtime') } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    build: { write: false, minify: false, lib: { entry, name: 'LibraryCards', formats: ['iife'] } },
  })
  const files = (Array.isArray(bundle) ? bundle : [bundle]).flatMap(item => item.output)
  const js = files.filter(file => file.type === 'chunk').map(file => file.code).join('\n')
  const css = files.filter(file => file.type === 'asset' && file.fileName.endsWith('.css')).map(file => file.source).join('\n')
  await writeFile(path.join(output, 'preview.html'), `<html data-theme-family="classic"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Library item pricing</title><style>${css}</style></head><body><div id="root"></div><script>${js.replaceAll('</script', '<\\/script')}</script></body></html>`)
  if (process.argv.includes('--build-only')) {
    console.log(`Offline preview built: ${path.join(output, 'preview.html')}`)
  } else {
    browser = await chromium.launch({ headless: true })
    const results = []
    for (const theme of ['classic', 'foundry']) {
      for (const width of [320, 390, 1280]) {
        const page = await browser.newPage({ viewport: { width, height: 900 } })
        const errors = []
        const requests = []
        page.on('pageerror', error => errors.push(error.message))
        await page.route('**/*', route => { requests.push(route.request().url()); return route.abort() })
        await page.setContent(`<html data-theme-family="${theme}"><head><style>${css}</style></head><body><div id="root"></div></body></html>`)
        await page.addScriptTag({ content: js })
        for (const c of cases) {
          const card = page.getByRole('region', { name: c.name, exact: true })
          await card.waitFor()
          assert.match(await card.innerText(), new RegExp(`Cost per item: \\$${c.expected.estimated_cost.toFixed(2).replace('.', '\\.')}`))
          assert.match(await card.innerText(), new RegExp(`Price per item: \\$${c.expected.suggested_price.toFixed(2).replace('.', '\\.')}`))
          assert.equal(await card.getByText('6 per plate', { exact: true }).count(), c.model.units_per_bed > 1 ? 1 : 0)
          const layout = await card.evaluate(element => {
            const box = element.getBoundingClientRect()
            const text = [...element.querySelectorAll('div, p')].filter(node => /^(Cost per item:|Price per item:|6 per plate)/.test(node.textContent) && node.childElementCount <= 1)
            return { overflow: element.scrollWidth > element.clientWidth + 1, clipped: text.some(node => {
              const range = document.createRange(); range.selectNodeContents(node)
              return [...range.getClientRects()].some(rect => rect.left < box.left - 1 || rect.right > box.right + 1)
            }) }
          })
          assert.deepEqual(layout, { overflow: false, clipped: false }, `${theme} ${width} ${c.name}`)
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
        assert.deepEqual(errors, [])
        assert.deepEqual(requests, [], 'Offline fixture must not request any URLs')
        const filename = `${theme}-${width}.png`
        await page.screenshot({ path: path.join(output, filename), fullPage: true })
        results.push({ theme, width, cards: cases.length, overflow: false, clipped: false, screenshot: filename })
        await page.close()
      }
    }
    await writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n')
    console.log(`PASS: ${results.length} theme/width checks, ${results.length * cases.length} cards. Evidence: ${output}`)
  }
} finally {
  await browser?.close()
  await rm(scratch, { recursive: true, force: true })
}
