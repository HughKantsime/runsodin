// Cert B: gcode_state values written by the non-Bambu monitors.
// Live evidence (tests/test_contracts/test_cert_B_live_stack.py::test_klipper_printing_state_as_written_by_monitor):
// the real Moonraker monitor stores gcode_state 'PRINTING' for an active Klipper print
// (backend/modules/printers/monitors/moonraker_monitor.py:239); PrusaLink/Elegoo parsers emit 'PRINTING' too.
// These tests pin that the TV dashboard and Camera Detail treat such printers as printing.
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TVDashboard from './TVDashboard'
import { ActiveJobPanel } from '../../components/printers/PrinterPanels'

const state = vi.hoisted(() => ({ printers: [] as any[] }))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('@tanstack/react-query', () => ({ useQuery: ({ queryKey }: any) => ({ data: queryKey[0] === 'printers' ? state.printers : { total: 0 }, isLoading: false, isError: false, refetch: vi.fn() }) }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'Cert farm' }) }))
vi.mock('../../hooks/useWebRTC', () => ({ default: () => ({ videoRef: { current: null }, status: 'connecting' }) }))

const klipper = () => ({ id: 2, name: 'CertB Klipper', last_seen: new Date().toISOString(), gcode_state: 'PRINTING', mc_percent: 35, mc_remaining_time: 18, gcode_file: 'certb_part.gcode' })
beforeEach(() => { state.printers = [klipper()] })
afterEach(() => cleanup())

// Was DEFECT (fixed by the shared print-state helper): frontend/src/pages/dashboard/TVDashboard.tsx:155 counts only RUNNING/PAUSE.
test('TV header counts a Klipper printer reporting PRINTING as printing', () => {
  render(<TVDashboard />)
  expect(screen.getByText('1/1 Printing')).toBeInTheDocument()
})

// Was DEFECT (fixed by the shared print-state helper): frontend/src/pages/dashboard/TVDashboard.tsx:40 shows status Idle and hides progress/ETA.
test('TV card shows Printing, progress and ETA for a PRINTING printer', () => {
  render(<TVDashboard />)
  expect(screen.getByTestId('tv-printer-status')).toHaveTextContent('Printing')
  expect(screen.getByTestId('tv-printer-progress')).toHaveTextContent('35%')
})

// Was DEFECT (fixed by the shared print-state helper): frontend/src/components/printers/PrinterPanels.tsx:101 renders no Current Job panel for PRINTING.
test('Camera Detail active-job panel renders for a PRINTING printer', () => {
  render(<ActiveJobPanel printer={klipper() as any} />)
  expect(screen.getByText('certb_part.gcode')).toBeInTheDocument()
})

test('control: the same printer reporting RUNNING (Bambu) is counted and shown', () => {
  state.printers = [{ ...klipper(), gcode_state: 'RUNNING' }]
  render(<TVDashboard />)
  expect(screen.getByText('1/1 Printing')).toBeInTheDocument()
  expect(screen.getByTestId('tv-printer-progress')).toHaveTextContent('35%')
})
