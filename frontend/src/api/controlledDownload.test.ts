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
test('signed audit export acknowledges with date bounds and bundle intact', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(refusal()).mockResolvedValueOnce(new Response('signed'))
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  await controlledDownload('/api/export/audit-logs/jsonl?date_from=2026-11-01&date_to=2026-11-06&bundle=true', 'signed audit export')
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('unredacted controlled details'))
  expect(fetch).toHaveBeenLastCalledWith('/api/export/audit-logs/jsonl/acknowledge?date_from=2026-11-01&date_to=2026-11-06&bundle=true', expect.objectContaining({ method: 'POST' }))
})

test('background backup preparation polls without repeating acknowledgement', async () => {
  vi.useFakeTimers()
  try {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(refusal())
      .mockResolvedValueOnce(new Response('{"status":"preparing"}', { status: 202 }))
      .mockResolvedValueOnce(new Response('ready'))
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const result = controlledDownload('/api/backups/owned.db?bundle=true', 'backup')
    await vi.advanceTimersByTimeAsync(2000)
    expect(await (await result).text()).toBe('ready')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(fetch.mock.calls[2]).toEqual(fetch.mock.calls[1])
  } finally {
    vi.useRealTimers()
  }
})
