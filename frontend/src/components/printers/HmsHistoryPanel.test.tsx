import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import HmsHistoryPanel from './HmsHistoryPanel'
import { printerTelemetry, printers } from '../../api'
import toast from 'react-hot-toast'
import { canDo } from '../../permissions'
vi.mock('../../api', () => ({ printers: { clearErrors: vi.fn() }, printerTelemetry: { hmsHistory: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn() }))
const history = { entries: [{ id: 1, printer_id: 1, source: 'mqtt', code: 'SYNTHETIC', message: 'Synthetic error', severity: 'error', occurred_at: '2026-10-11T12:00:00Z' }], frequency: { SYNTHETIC: 1 }, total: 1 }
beforeEach(() => { vi.mocked(canDo).mockReturnValue(true); vi.mocked(printerTelemetry.hmsHistory).mockResolvedValue(history) })
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('does not offer printer commands to a viewer', async () => {
 vi.mocked(canDo).mockReturnValue(false)
 render(<HmsHistoryPanel printerId={1} apiType="bambu" />)
 await screen.findByText('Top Recurring (1 total)')
 expect(screen.queryByRole('button', { name: 'Clear Errors' })).toBeNull()
})
it('shows a load failure without claiming there were no errors', async () => {
 vi.mocked(printerTelemetry.hmsHistory).mockRejectedValue(new Error('Synthetic failure'))
 render(<HmsHistoryPanel printerId={1} />)
 expect(await screen.findByRole('alert')).toBeTruthy()
 expect(screen.queryByText('No HMS errors in the last 30 days')).toBeNull()
})
it('ignores an old history response after switching printers', async () => {
 let finish: (value: any) => void = () => {}
 vi.mocked(printerTelemetry.hmsHistory).mockImplementation(id => id === 1 ? new Promise(resolve => { finish = resolve }) : Promise.resolve({ entries: [], frequency: {}, total: 0 }))
 const view = render(<HmsHistoryPanel printerId={1} />)
 view.rerender(<HmsHistoryPanel printerId={2} />)
 await screen.findByText('No HMS errors in the last 30 days')
 await act(async () => finish(history))
 expect(screen.queryByText('Top Recurring (1 total)')).toBeNull()
})

it('can retry history after a failed request', async () => {
 vi.mocked(printerTelemetry.hmsHistory).mockRejectedValueOnce(new Error('Synthetic failure')).mockResolvedValueOnce(history)
 render(<HmsHistoryPanel printerId={1} />)
 await screen.findByRole('alert')
 fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
 await screen.findByText('Top Recurring (1 total)')
 expect(screen.queryByRole('alert')).toBeNull()
})
it('ignores stale results after changing the history window', async () => {
 let finish: (value: any) => void = () => {}
 vi.mocked(printerTelemetry.hmsHistory).mockImplementation((_id, days) => days === 30 ? new Promise(resolve => { finish = resolve }) : Promise.resolve({ entries: [], frequency: {}, total: 0 }))
 render(<HmsHistoryPanel printerId={1} />)
 fireEvent.click(screen.getByRole('button', { name: '7d' }))
 await screen.findByText('No HMS errors in the last 7 days')
 await act(async () => finish(history))
 expect(screen.queryByText('Top Recurring (1 total)')).toBeNull()
})
it('reports failed clearing without discarding historical errors', async () => {
 vi.mocked(printers.clearErrors).mockRejectedValue(new Error('Synthetic failure'))
 const notify = vi.spyOn(toast, 'error')
 render(<HmsHistoryPanel printerId={1} apiType="bambu" />)
 fireEvent.click(await screen.findByRole('button', { name: 'Clear Errors' }))
 await waitFor(() => expect(notify).toHaveBeenCalledWith('Clear errors failed. Please try again.'))
 expect(screen.getByText('Top Recurring (1 total)')).toBeTruthy()
 notify.mockRestore()
})
it('blocks duplicate clear commands and allows retry after rejection', async () => {
 let reject: (reason: any) => void = () => {}
 vi.mocked(printers.clearErrors).mockImplementationOnce(() => new Promise((_resolve, failure) => { reject = failure })).mockResolvedValueOnce({ success: true } as any)
 const notify = vi.spyOn(toast, 'error')
 render(<HmsHistoryPanel printerId={1} apiType="bambu" />)
 const clear = await screen.findByRole('button', { name: 'Clear Errors' })
 fireEvent.click(clear); fireEvent.click(clear)
 expect(printers.clearErrors).toHaveBeenCalledTimes(1)
 expect((clear as HTMLButtonElement).disabled).toBe(true)
 await act(async () => reject(new Error('Synthetic failure')))
 fireEvent.click(await screen.findByRole('button', { name: 'Clear Errors' }))
 await waitFor(() => expect(printers.clearErrors).toHaveBeenCalledTimes(2))
 await waitFor(() => expect(printerTelemetry.hmsHistory).toHaveBeenCalledTimes(2))
 notify.mockRestore()
})
