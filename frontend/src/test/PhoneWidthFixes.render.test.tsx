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
import SpoolGrid from '../components/inventory/SpoolGrid'
import ExpandableNote from '../components/ui/ExpandableNote'
import SpoolCard from '../components/inventory/SpoolCard'
import SlotTile from '../components/foundry/SlotTile'
import PrinterCard from '../components/foundry/PrinterCard'
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
  afterEach(() => { cleanup(); vi.restoreAllMocks(); delete document.documentElement.dataset.themeFamily })

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
      await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${document.body.innerHTML}</body></html>`)
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
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(320)
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
    const snapshot = () => page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${document.body.innerHTML}</body></html>`)
    try {
      await page.route('**/*', route => route.abort())
      await snapshot()
      const title = page.locator('h3').filter({ hasText: 'Synthetic ASA-CF' })
      const nameElement = title.locator('span').first()
      expect(await nameElement.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('2')
      expect(await page.getByText('Deep blue', { exact: true }).isVisible()).toBe(true)
      const libraryName = page.locator('span.text-sm.break-words').filter({ hasText: name }).first()
      expect(await libraryName.evaluate(el => getComputedStyle(el).whiteSpace)).not.toBe('nowrap')
      if (family === 'foundry') {
        const label = page.locator('.f-slot-label')
        expect(await label.textContent()).toBe('ASA-CF')
        expect((await page.locator('.f-slot').boundingBox())!.height).toBe(92)
      }
      await page.setViewportSize({ width: 1440, height: 900 })
      expect(await nameElement.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('3')
      const note = page.locator('td p.whitespace-pre-wrap')
      const collapsedHeight = await note.evaluate(el => el.getBoundingClientRect().height)
      expect(await note.evaluate(el => getComputedStyle(el).webkitLineClamp)).toBe('2')
      expect(await note.evaluate(el => el.getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle(el).lineHeight) + 1)).toBe(true)
      const expand = page.getByRole('button', { name: 'Show full note' })
      expect((await expand.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      expect(await expand.evaluate(el => el.parentElement!.getBoundingClientRect().height)).toBe(20)
      fireEvent.click(screen.getByRole('button', { name: 'Show full note' }))
      await snapshot()
      expect(await note.textContent()).toBe(correction.note)
      expect(await note.evaluate(el => el.getBoundingClientRect().height)).toBe(collapsedHeight)
      const dialog = page.getByRole('dialog', { name: 'Full note' })
      expect(await dialog.locator('p').textContent()).toBe(correction.note)
      expect((await dialog.locator('p').boundingBox())!.width).toBeGreaterThan(400)
      expect(await dialog.evaluate(el => el.closest('td') === null)).toBe(true)
    } finally { await page.close() }
  })
  it.each([4, 16])('%i compact slots keep fixed tile and equal card heights at phone and desktop widths', async count => {
    document.documentElement.dataset.themeFamily = 'foundry'
    const slots = Array.from({ length: count }, (_, i) => ({ slot_number: i + 1, filament_type: 'ASA-CF', catalogue_name: `${name} ${i}` }))
    mount(<div className="f-card-grid">{['One', 'Two'].map((label, i) => <PrinterCard key={label} printer={{ id: i + 1, name: label, api_type: 'bambu', filament_slots: slots.map(slot => ({ ...slot, catalogue_name: i ? 'Short' : slot.catalogue_name })) } as any} />)}</div>)
    const page = await browser.newPage()
    try {
      await page.route('**/*', route => route.abort())
      for (const width of [390, 1440]) {
        await page.setViewportSize({ width, height: 900 })
        const snapshot = () => page.setContent(`<!doctype html><html data-theme-family="foundry"><head><style>${css}</style></head><body>${document.body.innerHTML}</body></html>`)
        await snapshot()
        const heights = await page.locator('.f-slot').evaluateAll(tiles => tiles.map(tile => tile.getBoundingClientRect().height))
        expect(heights).toEqual(Array(count * 2).fill(92))
        const cards = await page.locator('.f-card').evaluateAll(cards => cards.map(card => card.getBoundingClientRect().height))
        expect(cards[0]).toBe(cards[1])
        fireEvent.click(screen.getAllByRole('button', { name: 'Filaments' })[0])
        await snapshot()
        expect(await page.locator('.f-card').evaluateAll(cards => cards.map(card => card.getBoundingClientRect().height))).toEqual(cards)
        const labels = page.getByRole('dialog').locator('.f-slot-label')
        expect(await labels.evaluateAll(labels => labels.every(label => label.scrollHeight <= label.clientHeight + 1))).toBe(true)
        fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      }
    } finally { await page.close() }
  })

  it.each(['classic', 'foundry'])('%s: spool columns follow available width in both grids and wrap whole words', async family => {
    document.documentElement.dataset.themeFamily = family
    const spools = Array.from({ length: 6 }, (_, id) => ({ id, status: 'active', filament_brand: 'Bambu Lab', filament_name: 'Support for PLA', filament_material: 'PLA', color_name: 'White', remaining_weight_g: 1000, percent_remaining: 100 })) as any
    const page = await browser.newPage()
    try {
      await page.route('**/*', route => route.abort())
      for (const grouped of [false, true]) {
        const view = mount(<div data-foundry-page="/spools"><SpoolGrid spools={spools} sortBy="printer" groupByPrinter={grouped} searchQuery="" selectedSpools={new Set()} onToggleSelect={noop} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} /></div>)
        // Main content after sidebar/padding: 3 old tracks at 1024 left
        // 201px cards after the 28px selection gutter. Test the real constraint.
        for (const [viewport, available, columns] of [[390, 358, 1], [1024, 720, 2], [1440, 1136, 3]]) {
          await page.setViewportSize({ width: viewport, height: 900 })
          await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body><main style="width:${available}px">${view.container.innerHTML}</main></body></html>`)
          const grid = page.locator('main .grid').first()
          const widths = await grid.locator(':scope > div').evaluateAll(items => items.map(item => item.querySelector('[style]')!.getBoundingClientRect().width))
          expect(Math.min(...widths)).toBeGreaterThanOrEqual(300)
          expect(await grid.evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length)).toBe(columns)
          const title = page.locator('h3 span').filter({ hasText: 'Bambu Lab' }).first()
          expect(await title.evaluate(el => getComputedStyle(el).wordBreak)).toBe('normal')
          expect(await title.evaluate(el => getComputedStyle(el).overflowWrap)).toBe('break-word')
          // A word that fits must stay on a single line (range per character).
          expect(await title.evaluate(el => {
            const text = el.firstChild!
            const tops = Array.from({ length: 5 }, (_, i) => { const range = document.createRange(); range.setStart(text, i); range.setEnd(text, i + 1); return range.getBoundingClientRect().top })
            return new Set(tops).size
          })).toBe(1)
          expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport)
        }
        view.unmount()
      }
    } finally { await page.close() }
  })

  it.each(['classic', 'foundry'])('%s: all three disclosure close controls measure at least 44px at 1440', async family => {
    document.documentElement.dataset.themeFamily = family
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(320)
    const disclosures = [
      { title: 'Full note', trigger: 'Show full note', ui: <ExpandableNote text={name} /> },
      { title: 'Filaments', trigger: 'Filaments', ui: <PrinterCard printer={{ id: 1, name: 'Printer', api_type: 'bambu', filament_slots: [{ slot_number: 1, filament_type: 'PLA' }] } as any} /> },
      { title: 'Filament name', trigger: 'Show full filament name', ui: <SpoolCard spool={{ id: 1, filament_name: name, status: 'active' } as any} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} /> },
    ]
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    try {
      await page.route('**/*', route => route.abort())
      for (const { title, trigger, ui } of disclosures) {
        const view = mount(ui)
        fireEvent.click(screen.getByRole('button', { name: trigger }))
        await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${document.body.innerHTML}</body></html>`)
        const close = page.getByRole('dialog', { name: title }).getByRole('button', { name: 'Close', exact: true })
        const box = (await close.boundingBox())!
        expect(box.height, title).toBeGreaterThanOrEqual(44)
        expect(box.width, title).toBeGreaterThanOrEqual(44)
        view.unmount()
      }
    } finally { await page.close() }
  })

})
