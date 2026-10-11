import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import NozzleStatusCard from './NozzleStatusCard'
import { printerTelemetry } from '../../api'
import { canDo } from '../../permissions'
vi.mock('../../permissions', () => ({ canDo: vi.fn() }))
vi.mock('../../api', () => ({ printerTelemetry: { nozzle: vi.fn(), nozzleHistory: vi.fn(), installNozzle: vi.fn(), retireNozzle: vi.fn() } }))
beforeEach(() => { vi.mocked(canDo).mockReturnValue(true); vi.mocked(printerTelemetry.nozzle).mockResolvedValue(null) })
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('does not offer writes to a viewer', async () => {
  vi.mocked(canDo).mockReturnValue(false)
  render(<NozzleStatusCard printerId={1} />)
  await screen.findByText('No nozzle tracked')
  expect(screen.queryByRole('button', { name: 'Track Nozzle' })).toBeNull()
})
it('shows a failed load without claiming no nozzle exists', async () => {
  vi.mocked(printerTelemetry.nozzle).mockRejectedValue(new Error('Synthetic failure'))
  render(<NozzleStatusCard printerId={1} />)
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.queryByText('No nozzle tracked')).toBeNull()
})
it('keeps an unsuccessful install open and shows the failure', async () => {
  vi.mocked(printerTelemetry.installNozzle).mockRejectedValue(new Error('Synthetic failure'))
  render(<NozzleStatusCard printerId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Track Nozzle' }))
  fireEvent.click(screen.getByRole('button', { name: 'Install' }))
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Install' })).toBeTruthy()
})
it('prevents repeated submissions while installation is pending', async () => {
  vi.mocked(printerTelemetry.installNozzle).mockImplementation(() => new Promise(() => {}))
  render(<NozzleStatusCard printerId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Track Nozzle' }))
  const button = screen.getByRole('button', { name: 'Install' })
  fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(printerTelemetry.installNozzle).toHaveBeenCalledTimes(1))
  expect((button as HTMLButtonElement).disabled).toBe(true)
})
it('ignores an old printer response after switching printers', async () => {
  let finish: (value: any) => void = () => {}
  vi.mocked(printerTelemetry.nozzle).mockImplementation(id => id === 1 ? new Promise(resolve => { finish = resolve }) : Promise.resolve(null))
  const view = render(<NozzleStatusCard printerId={1} />)
  view.rerender(<NozzleStatusCard printerId={2} />)
  await screen.findByText('No nozzle tracked')
  const { act } = await import('@testing-library/react')
  await act(async () => finish({ id: 10, printer_id: 1, nozzle_type: 'Stale nozzle', print_hours_accumulated: 0, print_count: 0 }))
  expect(screen.queryByText('Stale nozzle')).toBeNull()
  expect(screen.getByText('No nozzle tracked')).toBeTruthy()
})
it('shows history failures to the user', async () => {
  vi.mocked(printerTelemetry.nozzle).mockResolvedValue({ id: 10, nozzle_type: 'brass', print_hours_accumulated: 0, print_count: 0 } as any)
  vi.mocked(printerTelemetry.nozzleHistory).mockRejectedValue(new Error('Synthetic failure'))
  render(<NozzleStatusCard printerId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: 'History' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load nozzle history')
})
it('can retry an install after failure and refresh the saved nozzle', async () => {
  vi.mocked(printerTelemetry.installNozzle).mockRejectedValueOnce(new Error('Synthetic failure')).mockResolvedValueOnce({} as any)
  render(<NozzleStatusCard printerId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Track Nozzle' }))
  fireEvent.click(screen.getByRole('button', { name: 'Install' }))
  await screen.findByRole('alert')
  vi.mocked(printerTelemetry.nozzle).mockResolvedValue({ id: 10, nozzle_type: 'Saved nozzle', print_hours_accumulated: 0, print_count: 0 } as any)
  fireEvent.click(screen.getByRole('button', { name: 'Install' }))
  await screen.findByText('Saved nozzle')
  expect(printerTelemetry.installNozzle).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).toBeNull()
})
it('does not reveal history from a previously selected printer', async () => {
  let finish: (value: any) => void = () => {}
  vi.mocked(printerTelemetry.nozzle).mockResolvedValue({ id: 10, nozzle_type: 'brass', print_hours_accumulated: 0, print_count: 0 } as any)
  vi.mocked(printerTelemetry.nozzleHistory).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const view = render(<NozzleStatusCard printerId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: 'History' }))
  view.rerender(<NozzleStatusCard printerId={2} />)
  await screen.findByRole('button', { name: 'History' })
  const { act } = await import('@testing-library/react')
  await act(async () => finish([{ id: 20, nozzle_type: 'Private old history', print_hours_accumulated: 0, print_count: 0 }]))
  expect(screen.queryByText(/Private old history/)).toBeNull()
})
it('shows unknown usage honestly', async () => {
  vi.mocked(printerTelemetry.nozzle).mockResolvedValue({ id: 10, nozzle_type: 'brass', print_hours_accumulated: null, print_count: null } as any)
  render(<NozzleStatusCard printerId={1} />)
  await screen.findByText('brass')
  expect(screen.getAllByText('Unknown').length).toBeGreaterThanOrEqual(2)
  expect(screen.queryByText('0.0h')).toBeNull()
})

it('preserves recorded zero in current and history displays', async () => {
  const nozzle = { id: 10, nozzle_type: 'brass', print_hours_accumulated: 0, print_count: 0 }
  vi.mocked(printerTelemetry.nozzle).mockResolvedValue(nozzle as any)
  vi.mocked(printerTelemetry.nozzleHistory).mockResolvedValue([nozzle] as any)
  render(<NozzleStatusCard printerId={1} />)
  await screen.findByText('brass')
  expect(screen.getByText('0.0h')).toBeTruthy()
  expect(screen.getByText('0')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'History' }))
  expect(await screen.findByText('0.0h / 0 prints')).toBeTruthy()
})
