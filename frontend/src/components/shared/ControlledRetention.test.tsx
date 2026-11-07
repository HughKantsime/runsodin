import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import ControlledRetention from './ControlledRetention'
const { fetchAPI } = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('../../api/client', () => ({ fetchAPI }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
test('acknowledges deleted-job policies returned by the preview', async () => {
  fetchAPI.mockResolvedValueOnce({ files: [4], jobs: [], tombstones: [{ job_id: 9, audit_max_id: 42 }], due_count: 1, due_timelapse_ids: [10] })
    .mockResolvedValueOnce({ acknowledged: true })
  render(<QueryClientProvider client={new QueryClient()}><ControlledRetention /></QueryClientProvider>)
  fireEvent.click(screen.getByText('Preview timelapse cleanup'))
  fireEvent.click(await screen.findByText('Acknowledge and enable cleanup'))
  await waitFor(() => expect(fetchAPI).toHaveBeenLastCalledWith('/controlled-parts/retention/acknowledge', {
    method: 'POST', headers: { 'X-ODIN-Controlled-Ack': 'true' }, body: JSON.stringify({ files: [4], jobs: [], tombstones: [{ job_id: 9, audit_max_id: 42 }] }),
  }))
  expect(await screen.findByRole('status')).toHaveTextContent('Cleanup enabled')
})
