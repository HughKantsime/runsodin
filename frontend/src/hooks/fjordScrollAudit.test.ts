import { expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'

it('scans overlapping viewports of main through the bottom, retaining offscreen failures and restoring scroll', async () => {
  const { scanMainRegion } = await import('../../../tests/accessibility/scroll_main.mjs')
  const main = { scrollTop: 75, clientHeight: 100, scrollHeight: 350 }
  const page = {
    locator: vi.fn(() => ({ evaluate: async (fn: any, arg: any) => fn(main, arg) })),
    evaluate: vi.fn().mockResolvedValue(undefined),
  }
  const positions: number[] = []
  const scans = await scanMainRegion(page, async () => {
    positions.push(main.scrollTop)
    return { violations: main.scrollTop === 250 ? [{ id: 'color-contrast' }] : [] }
  })
  expect(page.locator).toHaveBeenCalledWith('main')
  expect(positions).toEqual([0, 80, 160, 240, 250])
  expect(scans.at(-1)).toEqual({ scrollTop: 250, result: { violations: [{ id: 'color-contrast' }] } })
  expect(main.scrollTop).toBe(75)
})
it('scans short pages once and restores main when analysis fails', async () => {
  const { scanMainRegion } = await import('../../../tests/accessibility/scroll_main.mjs')
  const main = { scrollTop: 0, clientHeight: 100, scrollHeight: 100 }
  const page = { locator: () => ({ evaluate: async (fn: any, arg: any) => fn(main, arg) }), evaluate: vi.fn().mockResolvedValue(undefined) }
  const analyze = vi.fn().mockResolvedValue({ violations: [] })
  await scanMainRegion(page, analyze)
  expect(analyze).toHaveBeenCalledTimes(1)
  main.scrollTop = 30
  await expect(scanMainRegion(page, async () => { throw Error('axe failed') })).rejects.toThrow('axe failed')
  expect(main.scrollTop).toBe(30)
})
it('wires scrolling and Settings General into the Fjord browser matrix', () => {
  const audit = readFileSync('../tests/accessibility/accessibility_audit.mjs', 'utf8')
  expect(audit).toContain("import { scanMainRegion } from './scroll_main.mjs'")
  expect(audit).toMatch(/id: 'A09', route: '\/settings', tab: 'General'.*family: 'fjord'.*themes: \['light', 'dark'\]/)
  expect(audit).toContain("family === 'fjord' ? await scanMainRegion(page, analyze)")
  expect(audit).toContain('scans.flatMap(')
})

it('covers every reviewed Fjord page and Settings tab with populated regression fixtures', () => {
  const audit = readFileSync('../tests/accessibility/accessibility_audit.mjs', 'utf8')
  for (const route of ['/alerts', '/products', '/archives', '/analytics', '/spools', '/models', '/maintenance', '/orders']) expect(audit).toContain(`route: '${route}', fixture: 'fjord.json'`)
  for (const tab of ['Integrations', 'Vigil AI', 'Logs', 'Branding', 'Notifications', 'Filaments', 'License']) expect(audit).toContain(`tab: '${tab}', fixture: 'fjord.json'`)
  expect(audit).toContain("row.tab === 'Logs'")
  expect(audit).toContain('I acknowledge logs may contain controlled part names and details.')
  const fixture = JSON.parse(readFileSync('../tests/accessibility/fixtures/fjord.json', 'utf8'))
  expect(fixture['GET /api/archives'].items[0].tags).toContain('review')
  expect(fixture['GET /api/vision/printers']).toEqual([{ printer_id: 1, camera_source: 'auto' }])
  expect(Object.keys(fixture['GET /api/analytics'].jobs_by_date)).not.toHaveLength(0)
  expect(fixture['GET /api/smtp-config'].enabled).toBe(false)
})
