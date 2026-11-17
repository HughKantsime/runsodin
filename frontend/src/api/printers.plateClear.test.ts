import { afterEach, expect, it, vi } from 'vitest'
import { printers } from './printers'

afterEach(() => vi.restoreAllMocks())

it.each([{ expected_release_job_id: null }, { expected_release_job_id: 803 }, { expected_has_waiting_job: true }])('sends the plate confirmation body %j', async expected => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"success":true}'))
  await printers.plateCleared(199, expected)
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/printers/199/plate-cleared', expect.objectContaining({ method: 'POST', body: JSON.stringify(expected) }))
})

it('retains the queue conflict sentence and state for explicit reconfirmation', async () => {
  const detail = { message: 'The queue changed since you looked. Check what is waiting and confirm again.',
    plate_clear: { needed: true, has_waiting_job: true, releases_job: { id: 804, name: 'New lid' }, last_print_ended_at: null } }
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 409 }))
  await expect(printers.plateCleared(199, { expected_release_job_id: null })).rejects.toMatchObject({ message: detail.message, status: 409, detail })
  expect(fetch).toHaveBeenCalledTimes(1)
})
