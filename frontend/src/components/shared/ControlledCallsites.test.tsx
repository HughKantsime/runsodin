import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import BackupRestore from '../admin/BackupRestore'
import TimelineJobDetail from '../jobs/TimelineJobDetail'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api', () => ({ jobs: { get: api.get }, printJobs: {} }))
vi.mock('../jobs/JobAmsMapping', () => ({ default: () => null }))
vi.mock('../jobs/JobTimeline', () => ({ default: () => null }))
vi.mock('./ControlledPart', () => ({ default: ({ kind, id }: { kind: string; id: number }) => <p>Controlled editor {kind} {id}</p> }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('Timeline renders the controlled editor for the loaded scheduled job', async () => {
  api.get.mockResolvedValue({ id: 73, item_name: 'Part', status: 'pending' })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <TimelineJobDetail block={{ job_id: 73, start: '2026-11-01T10:00:00Z', end: '2026-11-01T11:00:00Z' }} />
  </QueryClientProvider>)
  expect(await screen.findByText('Controlled editor jobs 73')).toBeInTheDocument()
  expect(api.get).toHaveBeenCalledWith(73)
})

test.each([true, false])('BackupRestore retries controlled upload only when confirmed=%s', async accepted => {
  const fetch = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'controlled_part_acknowledgement_required' } }), { status: 403 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'staged', restart_required: true })))
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(accepted)
  const restored = vi.fn()
  render(<BackupRestore onRestored={restored} />)
  fireEvent.change(screen.getByLabelText('Choose backup file'), { target: { files: [new File(['owned'], 'backup.db')] } })
  fireEvent.click(screen.getByRole('button', { name: 'Stage Restore' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.stringContaining('replaces controlled work')))
  if (accepted) {
    expect(await screen.findByRole('status')).toHaveTextContent('Restore validated and staged')
    expect(fetch).toHaveBeenCalledTimes(2)
    const [url, init] = fetch.mock.calls[1]
    expect(url).toBe('/api/backups/restore?acknowledge_controlled=true')
    expect(new Headers(init?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
    expect(init?.body).toBe(fetch.mock.calls[0][1]?.body)
    expect(restored).toHaveBeenCalledOnce()
  } else {
    await screen.findByRole('alert')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(restored).not.toHaveBeenCalled()
  }
})
