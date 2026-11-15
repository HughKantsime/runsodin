import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { chromium, type Browser } from '@playwright/test'
import postcss from 'postcss'
import tailwind from '@tailwindcss/postcss'
import CataloguePicker from '../components/inventory/CataloguePicker'
import FilamentCatalogueSettings from '../components/admin/FilamentCatalogueSettings'
import FilamentLibraryView from '../components/inventory/FilamentLibraryView'
import SpoolCard from '../components/inventory/SpoolCard'
import SlotTile from '../components/foundry/SlotTile'
import PrintFailureControl from '../components/jobs/PrintFailureControl'
import PrintLog from '../pages/analytics/PrintLog'
import { inspectFoundryLayout } from '../../../tests/foundry_browser/layout_checks.mjs'

vi.mock('../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ role: 'admin', group_id: null }) }))
vi.mock('../components/inventory/PurchaseEvidenceModal', () => ({ default: () => null }))
const name = 'ASA-CF Invented very long catalogue colour name'
const correction = { by: 'Operator', at: '2026-11-14T12:00:00Z', reason_label: 'Other', note: 'A detailed observation of the print. '.repeat(30) }
const noop = () => {}
let browser: Browser
let css: string
function mount(children: React.ReactNode, client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })) {
  return render(<QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>)
}

// Same opt-in as RestartQuestion.render.test.tsx: uses installed Chromium only,
// setContent rather than a server, and refuses all browser network requests.
describe.skipIf(process.env.ODIN_BROWSER_RENDER !== '1')('phone-width rendered layout', () => {
  beforeAll(async () => {
    const from = join(process.cwd(), 'src/index.css')
    css = (await postcss([tailwind()]).process(await readFile(from, 'utf8'), { from })).css
    browser = await chromium.launch({ headless: true })
  }, 30000)
  afterAll(async () => { await browser?.close() })
  afterEach(() => { cleanup(); delete document.documentElement.dataset.themeFamily })

  it.each(['classic', 'foundry'])('%s: 390px catalogue and failure form targets', async family => {
    document.documentElement.dataset.themeFamily = family
    const view = mount(<div className="p-4 space-y-4">
      <section data-check-targets><CataloguePicker filaments={[]} value="" onChange={noop} /></section>
      <section data-check-targets><FilamentCatalogueSettings /></section>
      <section data-check-targets><PrintFailureControl inline canCorrect printId={1} status="cancelled" endedAt={correction.at} /></section>
      <section data-check-targets><PrintFailureControl inline canCorrect printId={2} status="failed" correction={correction} /></section>
    </div>)
    fireEvent.click(screen.getByRole('button', { name: 'Mark as failed' }))
    fireEvent.click(screen.getByRole('button', { name: 'Back to cancelled' }))
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
    try {
      await page.route('**/*', route => route.abort())
      await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${view.container.innerHTML}</body></html>`)
      // The existing tap-target audit measures real control/label bounds, including
      // file choosers; use it rather than treating class names as measured pixels.
      expect((await page.evaluate(inspectFoundryLayout)).smallTargets).toEqual([])
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
      const note = page.getByLabel('Note (optional)')
      await note.focus()
      const spacing = await note.evaluate(el => {
        const label = document.querySelector(`label[for="${el.id}"]`)!
        return { gap: el.getBoundingClientRect().top - label.getBoundingClientRect().bottom,
          padding: parseFloat(getComputedStyle(el).paddingLeft) }
      })
      expect(spacing.gap).toBeGreaterThanOrEqual(4)
      expect(spacing.padding).toBeGreaterThan(0)
      expect((await note.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    } finally { await page.close() }
  })

  it.each(['classic', 'foundry'])('%s: wrapping at 390px and clamped notes at 1440px', async family => {
    document.documentElement.dataset.themeFamily = family
    const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
    client.setQueryData(['filaments'], [{ id: 1, name, brand: 'Synthetic', material: 'ASA-CF', color_hex: '123456' }])
    client.setQueryData(['printers-list'], [])
    client.setQueryData(['print-log', 1, '', '', ''], { total: 1, items: [{ id: 1, print_job_id: 1, status: 'failed', print_name: 'Test print', failure_correction: correction }] })
    const view = mount(<div className="p-4">
      <SpoolCard spool={{ id: 1, status: 'active', filament_brand: 'Synthetic', filament_name: name, color_name: 'Deep blue', filament_material: 'ASA-CF' } as any}
        onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />
      <FilamentLibraryView />
      {family === 'foundry' && <div style={{ width: 64 }}><SlotTile variant="compact" slot={{ slot_number: 1, filament_type: 'ASA-CF', catalogue_name: name }} /></div>}
      <PrintLog />
    </div>, client)
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
    const snapshot = () => page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${view.container.innerHTML}</body></html>`)
    try {
      await page.route('**/*', route => route.abort())
      await snapshot()
      const title = page.locator('h3').filter({ hasText: 'Synthetic ASA-CF' })
      const nameElement = family === 'foundry' ? title : title.locator('span').first()
      expect(await nameElement.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('2')
      expect(await page.getByText('Deep blue', { exact: true }).isVisible()).toBe(true)
      const libraryName = page.locator('span.text-sm.break-words').filter({ hasText: name }).first()
      expect(await libraryName.evaluate(el => getComputedStyle(el).whiteSpace)).not.toBe('nowrap')
      if (family === 'foundry') {
        const label = page.locator('.f-slot-label')
        expect(await label.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('2')
        fireEvent.click(screen.getByRole('button', { name: `Show full filament name: ${name} · Slot 1 · Loaded` }))
        await snapshot()
        expect(await label.evaluate(el => el.scrollHeight <= el.clientHeight + 1)).toBe(true)
      }
      await page.setViewportSize({ width: 1440, height: 900 })
      const note = page.locator('td p.whitespace-pre-wrap')
      const collapsedHeight = await note.evaluate(el => el.getBoundingClientRect().height)
      expect(await note.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('2')
      fireEvent.click(screen.getByRole('button', { name: 'Show full note' }))
      await snapshot()
      expect(await note.textContent()).toBe(correction.note)
      expect(await note.evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThan(collapsedHeight)
    } finally { await page.close() }
  })
})
