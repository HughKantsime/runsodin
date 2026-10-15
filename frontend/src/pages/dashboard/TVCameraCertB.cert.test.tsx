// Cert B: TV thumbnails for auto-configured Bambu cameras and disabled cameras (F02-2.6-04, F02-2.1-03).
// Printer rows mirror the /api/printers producer: the generated X1C/H2D camera URL comes back
// masked with camera_source 'auto' (tests/test_contracts/test_cert_B_camera_fixes.py::test_printers_report_camera_enabled_and_effective_camera).
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TVDashboard from './TVDashboard'

const state = vi.hoisted(() => ({ printers: [] as any[], streams: [] as number[] }))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('@tanstack/react-query', () => ({ useQuery: ({ queryKey }: any) => ({ data: queryKey[0] === 'printers' ? state.printers : { total: 0 }, isLoading: false, isError: false, refetch: vi.fn() }) }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'Cert farm' }) }))
vi.mock('../../hooks/useWebRTC', () => ({ default: (id: number) => { state.streams.push(id); return { videoRef: { current: null }, status: 'connecting' } } }))

const now = () => new Date().toISOString()
beforeEach(() => {
  state.streams = []
  state.printers = [
    { id: 21, name: 'X1C auto', last_seen: now(), gcode_state: 'IDLE', camera_url: 'rtsps://***@192.0.2.21:322/streaming/live/1', camera_source: 'auto', camera_enabled: true },
    { id: 23, name: 'Camera off', last_seen: now(), gcode_state: 'IDLE', camera_url: 'rtsp://192.0.2.23:8554/cam', camera_source: 'configured', camera_enabled: false },
    { id: 22, name: 'P1S', last_seen: now(), gcode_state: 'IDLE', camera_url: null, camera_source: null, camera_enabled: true },
  ]
})
afterEach(() => cleanup())

test('auto-configured Bambu camera gets a live thumbnail; disabled and absent cameras do not', () => {
  render(<TVDashboard />)
  expect(new Set(state.streams)).toEqual(new Set([21]))
  const cards = screen.getAllByTestId('tv-printer-card')
  const card = (id: number) => cards.find(c => c.getAttribute('data-printer-id') === String(id))!
  expect(card(21).querySelector('video')).not.toBeNull()
  expect(card(23).querySelector('video')).toBeNull()
  expect(card(22).querySelector('video')).toBeNull()
})
