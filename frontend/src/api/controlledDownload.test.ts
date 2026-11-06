import { afterEach, expect, test, vi } from 'vitest'
import { controlledDownload } from './controlledDownload'
afterEach(() => vi.restoreAllMocks())
const refusal = () => new Response(JSON.stringify({ detail: { code: 'controlled_part_acknowledgement_required' } }), { status: 403 })
test('backup download requires an explicit acknowledgement', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(refusal()).mockResolvedValueOnce(new Response('backup'))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  expect((await controlledDownload('/api/backups/owned.db', 'backup')).status).toBe(200)
  expect(fetch).toHaveBeenLastCalledWith('/api/backups/owned.db/acknowledge', { method: 'POST', credentials: 'include', headers: { 'X-ODIN-Controlled-Ack': 'true' } })
})
test('declining never retries a privileged download', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(refusal())
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  await expect(controlledDownload('/api/admin/support-bundle', 'support bundle')).rejects.toThrow('cancelled')
  expect(fetch).toHaveBeenCalledTimes(1)
})
test('acknowledgement preserves the audit-key bundle query', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(refusal()).mockResolvedValueOnce(new Response('bundle'))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await controlledDownload('/api/backups/owned.db?bundle=true', 'backup')
  expect(fetch).toHaveBeenLastCalledWith('/api/backups/owned.db/acknowledge?bundle=true', expect.objectContaining({ method: 'POST' }))
})
