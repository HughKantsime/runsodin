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
