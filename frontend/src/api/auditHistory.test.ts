import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { auditLogs } from './auth'
vi.mock('../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))
const transport = vi.fn()
beforeEach(() => { transport.mockReset(); vi.stubGlobal('fetch', transport) })
afterEach(() => vi.unstubAllGlobals())

test('list serializes literal search and returns the paged server contract', async () => {
  const page = { logs: [], total: 0, limit: 50, offset: 0 }
  transport.mockResolvedValue(new Response(JSON.stringify(page), { headers: { 'Content-Type': 'application/json' } }))
  expect(await auditLogs.list({ q: 'owned_%', offset: 0, limit: 0 })).toEqual(page)
  const url = new URL(transport.mock.calls[0][0], 'https://owned.invalid')
  expect(url.pathname).toBe('/api/audit-logs')
  expect(url.searchParams.get('q')).toBe('owned_%')
  expect(url.searchParams.get('offset')).toBe('0')
  expect(url.searchParams.get('limit')).toBe('0')
})

test('CSV sends search/date/entity/action but no pagination', async () => {
  transport.mockResolvedValue(new Response('ID,Action\n1,create\n', { headers: { 'Content-Type': 'text/csv; charset=utf-8' } }))
  expect(await auditLogs.exportCsv({ q: 'owned_%', date_from: '2026-10-12', date_to: '2026-10-13', entity_type: 'job', action: 'create', limit: 1, offset: 50 })).toContain('1,create')
  const url = new URL(transport.mock.calls[0][0], 'https://owned.invalid')
  expect(url.pathname).toBe('/api/export/audit-logs')
  expect(Object.fromEntries(url.searchParams)).toEqual({ q: 'owned_%', date_from: '2026-10-12', date_to: '2026-10-13', entity_type: 'job', action: 'create' })
  expect(transport.mock.calls[0][1].credentials).toBe('include')
})

test('CSV refuses permission errors and non-CSV success bodies', async () => {
  transport.mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Owned denied' }), { status: 403 }))
  await expect(auditLogs.exportCsv()).rejects.toThrow('Owned denied')
  transport.mockResolvedValueOnce(new Response('{}', { headers: { 'Content-Type': 'application/json' } }))
  await expect(auditLogs.exportCsv()).rejects.toThrow('Unexpected export response')
})
