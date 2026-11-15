// @vitest-environment node
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterAll, beforeAll, expect, it, vi } from 'vitest'
import { chromium, type Browser } from 'playwright'
import postcss from 'postcss'
import tailwind from '@tailwindcss/postcss'
import SpoolGrid from '../components/inventory/SpoolGrid'
import PrinterCard from '../components/foundry/PrinterCard'
import SlotTile from '../components/foundry/SlotTile'
import Modal from '../components/ui/Modal'
const theme = vi.hoisted(() => ({ foundry: false, phone: false }))
vi.mock('../hooks/themePreferences', () => ({ useFoundry: () => theme.foundry }))
vi.mock('../hooks/useMediaQuery', () => ({ useMediaQuery: () => theme.phone }))
vi.mock('../permissions', () => ({ canDo: () => true }))
vi.mock('../components/inventory/PurchaseEvidenceModal', () => ({ default: () => null }))
let browser: Browser, css: string
const noop = () => {}
beforeAll(async () => {
  const from = resolve('src/index.css')
  css = (await postcss([tailwind()]).process(readFileSync(from, 'utf8'), { from })).css
  browser = await chromium.launch({ headless: true })
}, 30000)
afterAll(async () => { await browser?.close() })
async function pageFor(ui: React.ReactNode, width: number, family: string) {
  theme.foundry = family === 'foundry'
  theme.phone = width < 640
  const html = renderToStaticMarkup(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { enabled: false } } })}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  await page.route('**/*', route => route.abort())
  await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body><main style="margin-left:${width >= 768 ? 256 : 0}px;padding:24px">${html}</main></body></html>`)
  return page
}
it.each(['classic', 'foundry'])('%s catalogue spool names fit three lines at 1440', async family => {
  const names = ['ASA-CF Black', 'Support for PLA/PETG Nature', 'TPU for AMS Orange', 'ASA Aero White']
  const page = await pageFor(<SpoolGrid spools={names.map((name, i) => ({ id: i, filament_brand: 'Bambu Lab', filament_name: name, filament_material: 'PLA', status: 'active' })) as any} sortBy="name" groupByPrinter={false} searchQuery="" selectedSpools={new Set()} onToggleSelect={noop} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />, 1440, family)
  try {
    const sizes = await page.locator('h3 > span:first-child').evaluateAll(nodes => nodes.map(n => ({ name: n.textContent, client: n.clientHeight, scroll: n.scrollHeight })))
    expect(sizes).toHaveLength(4)
    for (const size of sizes) expect(size.scroll, size.name!).toBeLessThanOrEqual(size.client + 1)
  } finally { await page.close() }
})
it('Foundry cards with the same slot count stretch equally despite a wrapped heading', async () => {
  const slots = Array.from({ length: 4 }, (_, i) => ({ slot_number: i + 1, filament_type: 'PLA' }))
  const page = await pageFor(<div className="f-card-grid">{['Bench X1C - left shelf by the window', 'Bench P1S', 'Bench X1C'].map((name, i) => <div className="relative" key={name}><PrinterCard printer={{ id: i, name, model: 'X1C', filament_slots: slots } as any} /></div>)}</div>, 1440, 'foundry')
  try {
    const heights = await page.locator('.f-card').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().height))
    expect(new Set(heights).size).toBe(1)
  } finally { await page.close() }
})
it.each(['', 'f-page'])('six compact material labels fit 57px tiles without ellipses (container=%s)', async className => {
  const page = await pageFor(<div className={className}><div style={{ width: 57 }}>{['ASA_CF', 'ASA_AERO', 'PET_CF', 'PPA_CF', 'PPS_CF', 'TPU_AMS'].map(code => <SlotTile key={code} variant="compact" slot={{ slot_number: 1, filament_type: code }} />)}</div></div>, 390, 'foundry')
  try {
    const sizes = await page.locator('.f-slot-label').evaluateAll(nodes => nodes.map(n => ({ label: n.textContent, client: n.clientWidth, scroll: n.scrollWidth })))
    expect(sizes.map(size => size.label)).toEqual(['ASA-CF', 'ASA Aero', 'PET-CF', 'PPA-CF', 'PPS-CF', 'TPU-AMS'])
    for (const size of sizes) expect(size.scroll, size.label!).toBeLessThanOrEqual(size.client)
  } finally { await page.close() }
})

// Exercise both grid branches and the sidebar's loss of usable width. A viewport
// breakpoint alone cannot guarantee the card width once the sidebar is present.
it.each(['classic', 'foundry'].flatMap(family => [false, true].map(grouped => ({ family, grouped }))))('$family spool cards keep 300px after the selection gutter (grouped=$grouped)', async ({ family, grouped }) => {
  for (const width of [390, 768, 1024, 1280, 1440]) {
    const spools = Array.from({ length: 6 }, (_, id) => ({ id, filament_name: 'Support for PLA/PETG Nature', status: 'active' }))
    const page = await pageFor(<SpoolGrid spools={spools as any} sortBy={grouped ? 'printer' : 'name'} groupByPrinter={grouped} searchQuery="" selectedSpools={new Set()} onToggleSelect={noop} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />, width, family)
    try {
      const cards = await page.locator('.grid > .relative > .pl-7 > div').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().width))
      expect(cards).toHaveLength(6)
      for (const card of cards) expect(card, `${family}, grouped=${grouped}, viewport=${width}`).toBeGreaterThanOrEqual(300)
      expect(await page.locator('body').evaluate(n => n.scrollWidth)).toBeLessThanOrEqual(width)
    } finally { await page.close() }
  }
})

it.each(['classic', 'foundry'])('%s modal close target stays inside the header and clear of wrapped titles', async family => {
  for (const width of [390, 1440]) {
    for (const alert of [false, true]) {
      for (const title of ['Filament name', 'Change the filament catalogue assignment for the printer beside the workshop window']) {
        const page = await pageFor(<Modal isOpen onClose={noop} title={title} alert={alert}>Body</Modal>, width, family)
        try {
          const bounds = await page.getByLabel('Close', { exact: true }).evaluate(button => {
            const rect = (node: Element) => {
              const { top, right, bottom, left, width, height } = node.getBoundingClientRect()
              return { top, right, bottom, left, width, height }
            }
            return { button: rect(button), header: rect(button.parentElement!), title: rect(button.parentElement!.querySelector('h2')!) }
          })
          expect(bounds.button.width).toBeGreaterThanOrEqual(44)
          expect(bounds.button.height).toBeGreaterThanOrEqual(44)
          expect(bounds.button.left).toBeGreaterThanOrEqual(bounds.header.left)
          expect(bounds.button.right).toBeLessThanOrEqual(bounds.header.right)
          expect(bounds.button.top).toBeGreaterThanOrEqual(bounds.header.top)
          expect(bounds.button.bottom).toBeLessThanOrEqual(bounds.header.bottom)
          expect(bounds.title.right).toBeLessThanOrEqual(bounds.button.left)
          if (width === 390 && title.length > 20) {
            const lines = await page.locator('h2').evaluate(n => n.getBoundingClientRect().height / parseFloat(getComputedStyle(n).lineHeight))
            expect(lines).toBeGreaterThan(1)
          }
        } finally { await page.close() }
      }
    }
  }
})

it.each(['classic', 'foundry'])('%s desktop header height is independent of the enlarged close target', async family => {
  const page = await pageFor(<Modal isOpen onClose={noop} title="Filament name">Body</Modal>, 1440, family)
  try {
    const heights = await page.getByLabel('Close', { exact: true }).evaluate(button => {
      const header = button.parentElement!
      const enlarged = header.getBoundingClientRect().height
      // Recreate the original 20px icon-only target with no negative margins.
      Object.assign((button as HTMLElement).style, { minHeight: '20px', minWidth: '20px', height: '20px', width: '20px', margin: '0' })
      return { enlarged, original: header.getBoundingClientRect().height }
    })
    expect(heights.enlarged).toBe(heights.original)
  } finally { await page.close() }
})
