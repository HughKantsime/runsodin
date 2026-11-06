import { afterEach, expect, it, vi } from 'vitest'
vi.mock('./client', () => ({ fetchAPI: vi.fn(), downloadBlob: vi.fn() }))
import { fetchAPI, downloadBlob } from './client'
import { traceability } from './traceability'
afterEach(() => vi.clearAllMocks())
it('uses exact traceability routes and a verifiable certificate bundle', async () => {
  await traceability.get(4)
  expect(fetchAPI).toHaveBeenLastCalledWith('/jobs/4/traceability')
  await traceability.inspect(4, 'fail', 'Check hole diameter')
  expect(fetchAPI).toHaveBeenLastCalledWith('/jobs/4/inspection', { method: 'PUT', body: JSON.stringify({ result: 'fail', notes: 'Check hole diameter' }) })
  await traceability.deliver(4)
  expect(fetchAPI).toHaveBeenLastCalledWith('/jobs/4/deliver', { method: 'POST' })
  await traceability.certificate(4)
  expect(downloadBlob).toHaveBeenLastCalledWith('/jobs/4/certificate?bundle=true', 'job-4-certificate.zip')
  await traceability.batch('2026-11-01', '2026-11-02')
  expect(downloadBlob).toHaveBeenLastCalledWith(`/jobs/traceability/export?${new URLSearchParams({ start: new Date('2026-11-01T00:00:00').toISOString(), end: new Date('2026-11-02T00:00:00').toISOString() })}`, 'traceability.zip')
})
it('uses local calendar boundaries across the daylight-saving change', async () => {
  const previous = process.env.TZ
  process.env.TZ = 'America/New_York'
  try {
    await traceability.batch('2026-11-01', '2026-11-02')
    expect(downloadBlob).toHaveBeenLastCalledWith(
      '/jobs/traceability/export?start=2026-11-01T04%3A00%3A00.000Z&end=2026-11-02T05%3A00%3A00.000Z',
      'traceability.zip',
    )
  } finally {
    if (previous === undefined) delete process.env.TZ
    else process.env.TZ = previous
  }
})
