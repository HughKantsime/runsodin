import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const discover = vi.hoisted(() => vi.fn())
vi.mock('../../api', () => ({ printers: { discoverMoonraker: discover }, getPlugConfig: vi.fn(() => Promise.resolve(null)) }))
vi.mock('../../permissions', () => ({ canDo: () => true }))

import MoonrakerDiscovery from './MoonrakerDiscovery'
import PrinterModal from './PrinterModal'

const found = (extra = {}) => ({
  name: 'Voron', host: 'voron.local', ip: '192.168.50.13', port: 7125, address: '192.168.50.13:7125',
  already_configured: false, confirmed: true, klippy_state: 'ready', hostname: 'voron', ...extra,
})

beforeEach(() => discover.mockReset())
afterEach(cleanup)

it('lists results and passes the clicked address, disabling printers already added', async () => {
  discover.mockResolvedValue({ reason: null, printers: [found(), found({ name: 'Known', ip: '192.168.50.10', address: '192.168.50.10:7125', already_configured: true, klippy_state: null })] })
  const onPick = vi.fn()
  render(<MoonrakerDiscovery onPick={onPick} />)
  fireEvent.click(screen.getByRole('button', { name: 'Find Klipper printers on the network' }))
  const result = await screen.findByRole('button', { name: /192\.168\.50\.13:7125/ })
  expect(result).toHaveTextContent('Klipper: ready')
  expect(screen.getByRole('button', { name: /192\.168\.50\.10:7125/ })).toBeDisabled()
  fireEvent.click(result)
  expect(onPick).toHaveBeenCalledExactlyOnceWith('192.168.50.13:7125')
})

it('shows the plain reason for an empty result instead of an error', async () => {
  discover.mockResolvedValue({ printers: [], reason: 'Discovery needs the container on the host network or mDNS reflection.' })
  render(<MoonrakerDiscovery onPick={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Find Klipper printers on the network' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Discovery needs the container on the host network or mDNS reflection.')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('reports a failed request without throwing', async () => {
  discover.mockResolvedValue(undefined)   // an unusable answer takes the same failure path as a refused request
  render(<MoonrakerDiscovery onPick={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Find Klipper printers on the network' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Discovery failed')
})

it('is offered in the add-printer dialog only for Moonraker and fills the host field', async () => {
  discover.mockResolvedValue({ reason: null, printers: [found()] })
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={vi.fn()} />)
  const protocol = screen.getByRole('option', { name: 'Klipper / Moonraker' }).closest('select')!
  expect(screen.queryByRole('button', { name: 'Find Klipper printers on the network' })).not.toBeInTheDocument()
  fireEvent.change(protocol, { target: { value: 'bambu' } })
  expect(screen.queryByRole('button', { name: 'Find Klipper printers on the network' })).not.toBeInTheDocument()
  fireEvent.change(protocol, { target: { value: 'moonraker' } })
  fireEvent.click(screen.getByRole('button', { name: 'Find Klipper printers on the network' }))
  fireEvent.click(await screen.findByRole('button', { name: /192\.168\.50\.13:7125/ }))
  expect(screen.getByPlaceholderText('e.g., 192.168.1.100')).toHaveValue('192.168.50.13:7125')
})

it('is not offered when editing an existing printer', () => {
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={vi.fn()} printer={{ id: 7, name: 'x', model: 'Voron', slot_count: 1, api_type: 'moonraker', api_host: '192.168.50.9', tags: [] }} />)
  expect(screen.queryByRole('button', { name: 'Find Klipper printers on the network' })).not.toBeInTheDocument()
})
