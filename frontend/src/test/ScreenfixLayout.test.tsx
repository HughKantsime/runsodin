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
const theme = vi.hoisted(() => ({ foundry: false }))
vi.mock('../hooks/themePreferences', () => ({ useFoundry: () => theme.foundry }))
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
  const html = renderToStaticMarkup(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { enabled: false } } })}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  await page.route('**/*', route => route.abort())
  await page.setContent(`<html data-theme-family="${family}"><head><style>${css}</style></head><body><main style="margin-left:${width > 800 ? 256 : 0}px;padding:24px">${html}</main></body></html>`)
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
it('six compact material labels fit 57px tiles without ellipses', async () => {
  const page = await pageFor(<div style={{ width: 57 }}>{['ASA_CF', 'ASA_AERO', 'PET_CF', 'PPA_CF', 'PPS_CF', 'TPU_AMS'].map(code => <SlotTile key={code} variant="compact" slot={{ slot_number: 1, filament_type: code }} />)}</div>, 390, 'foundry')
  try {
    const sizes = await page.locator('.f-slot-label').evaluateAll(nodes => nodes.map(n => ({ label: n.textContent, client: n.clientWidth, scroll: n.scrollWidth })))
    for (const size of sizes) expect(size.scroll, size.label!).toBeLessThanOrEqual(size.client)
  } finally { await page.close() }
})
