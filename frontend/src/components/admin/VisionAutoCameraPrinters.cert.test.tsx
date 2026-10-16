// Cert C (settings gap) — auto-configured cameras (no stored camera_url) are tunable per printer.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import VisionSettingsTab from './VisionSettingsTab'

const api = vi.hoisted(() => ({ list: vi.fn(), cams: vi.fn(), printerGet: vi.fn(), printerSet: vi.fn() }))
vi.mock('../../api', () => ({
  vision: { getSettings: vi.fn().mockResolvedValue({ enabled: true, retention_days: 30 }), updateSettings: vi.fn(), monitorStatus: vi.fn().mockResolvedValue(null),
            cameraPrinters: api.cams, getPrinterSettings: api.printerGet, updatePrinterSettings: api.printerSet },
  printers: { list: api.list },
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, role: 'admin', group_id: 1 }) }))
vi.mock('./VisionModelUpload', () => ({ default: () => null }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('lists an auto-camera X1C next to a configured camera and saves its settings', async () => {
  api.list.mockResolvedValue([
    { id: 9, name: 'Configured cam', camera_url: 'rtsp://cam' },
    { id: 21, name: 'X1C auto', camera_url: null, model: 'X1C' },
    { id: 23, name: 'P1S no camera', camera_url: null, model: 'P1S' },
  ])
  api.cams.mockResolvedValue([{ printer_id: 9, camera_source: 'configured' }, { printer_id: 21, camera_source: 'auto' }])
  api.printerGet.mockResolvedValue({ enabled: 1, spaghetti_threshold: 0.65, first_layer_threshold: 0.6, detachment_threshold: 0.7 })
  api.printerSet.mockResolvedValue({})
  render(<VisionSettingsTab />)
  const auto = await screen.findByText('built-in camera (auto)')
  expect(screen.getByText('Configured cam')).toBeInTheDocument()
  expect(screen.queryByText('P1S no camera')).toBeNull()
  await waitFor(() => expect(api.printerGet.mock.calls.map((call: unknown[]) => Number(call[0])).sort((a: number, b: number) => a - b)).toEqual([9, 21]))
  const card = auto.closest('div.rounded-md') as HTMLElement
  fireEvent.click(within(card).getByRole('checkbox', { name: 'Enabled' }))
  await waitFor(() => expect(api.printerSet).toHaveBeenCalledExactlyOnceWith(21, { enabled: 0 }))
})
