import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import InstalledNozzles from './InstalledNozzles'
afterEach(cleanup)
it('shows each physical nozzle, flow, material, wear and observation timestamp', () => {
  const { container } = render(<InstalledNozzles nozzles={[
    {extruder: 0, side:'right', diameter:0.4, flow:'High Flow', material:'Tungsten carbide', wear:0, observed_at:'2026-10-27T12:00:00Z'},
    {extruder: 1, side:'left', diameter:0.6, flow:'Standard', material:'Hardened steel', wear:0.2, observed_at:'2026-10-27T12:01:00Z'},
  ]} />)
  expect(screen.getByText('left nozzle')).toBeInTheDocument()
  expect(screen.getByText('0.4 mm · High Flow · Tungsten carbide')).toBeInTheDocument()
  expect(screen.getByText('Wear: 0')).toBeInTheDocument()
  expect(container.querySelectorAll('time')[1].dateTime).toBe('2026-10-27T12:01:00Z')
})
it('does not claim unknown nozzles were checked', () => {
  render(<InstalledNozzles />)
  expect(screen.getByText(/No nozzle report received/)).toBeInTheDocument()
})

it('shows an unrecognized rack tool code as reported', () => {
  render(<InstalledNozzles rack nozzles={[{ nozzle_id: 20, position: 5, side: 'rack', type: 'ZZ99',
    virtual_slot_id: 251, diameter: null, material: null, flow: null, wear: null, observed_at: '2026-11-21T19:40:00Z' }]} />)
  const row = screen.getByRole('listitem')
  expect(row).toHaveTextContent('Position 5 · ZZ99 · Diameter unknown')
  expect(row).toHaveTextContent('Nothing reported')
  expect(row.querySelector('small')).toHaveTextContent('ID 251')
  expect(row).not.toHaveTextContent('In the tool head')
})
