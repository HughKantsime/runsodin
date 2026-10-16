// Cert C (final review MEDIUM-3) — vision settings show the concurrent-thread cap and save a new limit.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import VisionSettingsTab from './VisionSettingsTab'

const api = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn(), status: vi.fn(), list: vi.fn() }))
vi.mock('../../api', () => ({ vision: { getSettings: api.get, updateSettings: api.set, monitorStatus: api.status, getPrinterSettings: vi.fn(), updatePrinterSettings: vi.fn() }, printers: { list: api.list } }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, role: 'admin', group_id: 2 }) }))
vi.mock('./VisionModelUpload', () => ({ default: () => null }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows that the cap is reached and saves a raised limit', async () => {
  api.get.mockResolvedValue({ enabled: true, retention_days: 30 })
  api.status.mockResolvedValue({ max_threads: 16, reported: true, active_threads: 16, capped: true, capped_count: 3, capped_printer_ids: [41, 42] })
  api.list.mockResolvedValue([]); api.set.mockResolvedValue({})
  render(<VisionSettingsTab />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Vision thread cap reached (16 of 16 running): 3 printers waiting (IDs 41, 42)')
  const limit = screen.getByLabelText('Max printers monitored at once:')
  expect(limit).toHaveValue(16)
  fireEvent.change(limit, { target: { value: '24' } })
  fireEvent.click(screen.getByRole('button', { name: /Save/ }))
  await waitFor(() => expect(api.set).toHaveBeenCalledExactlyOnceWith({ enabled: true, retention_days: 30, max_threads: 24 }))
})

it('shows no cap warning while under the limit', async () => {
  api.get.mockResolvedValue({ enabled: true, retention_days: 30 })
  api.status.mockResolvedValue({ max_threads: 16, reported: true, active_threads: 2, capped: false, capped_count: 0, capped_printer_ids: [] })
  api.list.mockResolvedValue([])
  render(<VisionSettingsTab />)
  await waitFor(() => expect(api.status).toHaveBeenCalled())
  expect(screen.queryByRole('alert')).toBeNull()
})
