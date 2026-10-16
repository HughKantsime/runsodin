// Cert C (F03-3.2) — real Detections page wired to captured /api/vision/detections + /api/vision/stats responses.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Detections from './Detections'
import fixture from '../../test-fixtures/cert-C-vision-detections-wire.json'

const api = vi.hoisted(() => ({ get: vi.fn(), review: vi.fn(), stats: vi.fn() }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, vision: { getDetections: api.get, getStats: api.stats, reviewDetection: api.review, bulkReview: vi.fn() }, printers: { list: vi.fn().mockResolvedValue([{ id: 9, name: 'P1S-09' }, { id: 10, name: 'P1S-10' }]) } }))
vi.mock('react-hot-toast', () => ({ default: { error: vi.fn(), success: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => action === 'printers.edit' }))
// Image acquisition (JPEG fetch/hash) is a browser boundary covered by maintenanceMedia tests.
vi.mock('../../components/vision/BoundedVisionImage', () => ({ default: ({ path, alt, className, onClick }: any) => <img src={'/api/vision/frames/' + path} alt={alt} className={className} onClick={onClick} /> }))

beforeEach(() => { api.stats.mockResolvedValue(fixture.stats); api.get.mockResolvedValue(fixture.detections); api.review.mockResolvedValue({}) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, username: 'cert-admin', role: 'admin', group_id: null })
  return render(<QueryClientProvider client={client}><MemoryRouter><Detections /></MemoryRouter></QueryClientProvider>)
}

test('F03-3.2-01 grid and list views render every returned detection frame', async () => {
  mount()
  const thumbs = await screen.findAllByRole('img', { name: 'Detection thumbnail' })
  expect(thumbs.map(t => t.getAttribute('src'))).toEqual(fixture.detections.items.map((d: any) => '/api/vision/frames/' + d.frame_path))
  fireEvent.click(screen.getByRole('button', { name: 'List view' }))
  expect(screen.getByRole('button', { name: 'List view' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getAllByRole('img', { name: 'Detection thumbnail' })).toHaveLength(5)
})

test('F03-3.3-05 stat bar shows real windowed totals and confirmed review rate', async () => {
  mount()
  expect((await screen.findAllByText('5 detections')).length).toBeGreaterThan(0)
  expect(screen.getByText('50%')).toBeInTheDocument()
  expect(screen.getByText('(2 reviewed)')).toBeInTheDocument()
})

test('F03-3.2-02 filter selections are sent as the backend query parameters', async () => {
  mount()
  await screen.findAllByRole('img', { name: 'Detection thumbnail' })
  const selects = screen.getAllByRole('combobox')
  fireEvent.change(selects[1], { target: { value: 'auto_paused' } })
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith({ limit: 30, offset: 0, status: 'auto_paused' }))
})

test('F03-3.2-03/04 frame viewer opens the real record and confirm sends its id', async () => {
  mount()
  const thumbs = await screen.findAllByRole('img', { name: 'Detection thumbnail' })
  fireEvent.click(thumbs[0])
  expect(screen.getByRole('img', { name: 'Detection frame' })).toHaveAttribute('src', '/api/vision/frames/' + fixture.detections.items[0].frame_path)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Issue' }))
  await waitFor(() => expect(api.review).toHaveBeenCalledWith(fixture.detections.items[0].id, 'confirmed'))
})
