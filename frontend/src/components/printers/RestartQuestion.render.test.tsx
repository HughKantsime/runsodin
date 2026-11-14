import React from 'react'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { chromium, type Browser } from '@playwright/test'
import postcss from 'postcss'
import tailwind from '@tailwindcss/postcss'
import DispatchRecoveryPanel from './DispatchRecoveryPanel'
import { printers } from '../../api'
import { canDo } from '../../permissions'

vi.mock('../../api', () => ({ printers: { dispatchRecovery: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn() }))

let browser: Browser
let css: string
// Native browser launch is unavailable in restricted macOS sandboxes.
describe.skipIf(process.env.ODIN_BROWSER_RENDER !== '1')('native layout', () => {
  beforeAll(async () => {
    // Compile the real styles and render locally: no server or external requests.
    const from = join(process.cwd(), 'src/index.css')
    css = (await postcss([tailwind()]).process(await readFile(from, 'utf8'), { from })).css
    browser = await chromium.launch({ headless: true })
  }, 30000)
  afterAll(async () => { await browser?.close() })
  afterEach(() => { cleanup(); vi.resetAllMocks(); document.documentElement.removeAttribute('data-theme-family') })

  it.each([
    ['classic', 'operator'], ['classic', 'viewer'],
    ['foundry', 'operator'], ['foundry', 'viewer'],
  ])('renders both answers on a phone in %s for %s', async (family, role) => {
    document.documentElement.dataset.themeFamily = family
    vi.mocked(canDo).mockImplementation(action => role === 'operator' && action === 'printers.answer_restart_question')
    vi.mocked(printers.dispatchRecovery).mockResolvedValue({
      state: 'investigation_required', release_available: false,
      restart_question: { id: 'a'.repeat(32), state: 'pending', print_job_id: 241,
        raised_at: '2026-11-13T12:00:00Z', print_name: 'Bracket', reported_name: 'Bracket' },
      answer_restart_question: role === 'operator',
      actions: { hold: false, reconcile_retain: false, release: false },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(<QueryClientProvider client={client}><DispatchRecoveryPanel printerId={9} /></QueryClientProvider>)
    await screen.findByRole('region', { name: 'Confirm print after restart' })
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } })
    try {
      await page.route('**/*', route => route.abort())
      await page.setContent(`<!doctype html><html data-theme-family="${family}"><head><style>${css}</style></head><body>${view.container.innerHTML}</body></html>`)
      for (const name of ['Yes, same print: keep recording on it', 'No, it is a new print']) {
        const button = page.getByRole('button', { name, exact: true })
        expect(await button.isVisible()).toBe(true)
        expect(await button.isEnabled()).toBe(role === 'operator')
        const bounds = (await button.boundingBox())!
        expect(bounds.x).toBeGreaterThanOrEqual(0)
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(375)
        expect(bounds.y + bounds.height).toBeLessThanOrEqual(812)
        expect(bounds.height).toBeGreaterThanOrEqual(44)
        expect(await button.evaluate(node => getComputedStyle(node.parentElement!).flexDirection)).toBe('column')
        if (family === 'foundry') expect(await button.getAttribute('data-foundry-ui')).toBe('button')
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
      expect(await page.getByRole('button', { name: 'Release fence' }).count()).toBe(0)
      await page.screenshot({ path: join(tmpdir(), `odin-restart-${family}-${role}.png`), fullPage: true })
    } finally {
      await page.close()
    }
  })
})
