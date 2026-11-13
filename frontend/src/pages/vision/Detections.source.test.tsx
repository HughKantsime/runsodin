// Printer-camera detections on the Detections page: source marker, source filter, the printer's
// own code and sentence. Same page in Classic and Foundry; wraps instead of overflowing on a phone.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { applyTheme } from '../../hooks/themePreferences'
import Detections from './Detections'

const api = vi.hoisted(() => ({ get: vi.fn(), review: vi.fn(), stats: vi.fn() }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, vision: { getDetections: api.get, getStats: api.stats, reviewDetection: api.review }, printers: { list: vi.fn().mockResolvedValue([{ id: 9, name: 'P1S-09' }]) } }))
vi.mock('react-hot-toast', () => ({ default: { error: vi.fn(), success: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => action === 'printers.edit' }))
vi.mock('../../components/vision/BoundedVisionImage', () => ({ default: ({ path, alt }: any) => <img src={'/api/vision/frames/' + path} alt={alt} /> }))

const SENTENCE = 'Synthetic printer sentence about possible spaghetti.'
const row = (extra: Record<string, unknown>) => ({ printer_id: 9, print_job_id: 201, confidence: 1, frame_path: null, bbox_json: null, metadata_json: null, reviewed_by: null, reviewed_at: null, created_at: '2026-11-13T12:00:00', printer_name: 'P1S-09', ...extra })
const items = [
  row({ id: 1, detection_type: 'spaghetti', status: 'informational', source: 'printer', metadata_json: JSON.stringify({ source: 'printer', code: '0C00030000030008', text: SENTENCE }) }),
  row({ id: 2, detection_type: 'chute_pileup', status: 'pending', source: 'printer', frame_path: '9/2.jpg', metadata_json: '{not json' }),
  row({ id: 3, detection_type: 'spaghetti', status: 'pending', source: 'vigil', confidence: 0.82, frame_path: '9/3.jpg' }),
  row({ id: 4, detection_type: 'detachment', status: 'confirmed', confidence: 0.71, frame_path: '9/4.jpg' }), // older server: no source field
]

beforeEach(() => {
  api.stats.mockResolvedValue({ days: 7, by_type: { spaghetti: 2 }, by_source: { printer: { total: 2, confirmed: 0, dismissed: 0 }, vigil: { total: 2, confirmed: 1, dismissed: 0 } }, accuracy_pct: null, total_reviewed: 0 })
  api.get.mockResolvedValue({ items, total: items.length })
  api.review.mockResolvedValue({})
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); applyTheme('dark', 'classic') })

function mount(url = '/detections') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, username: 'admin', role: 'admin', group_id: null })
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[url]}><Detections /></MemoryRouter></QueryClientProvider>)
}
function theme(family: 'classic' | 'foundry') {
  localStorage.setItem('odin-theme-family', family)
  localStorage.setItem('odin-theme', 'dark')
  applyTheme('dark', family)
}

describe.each(['classic', 'foundry'] as const)('%s Detections page', family => {
  beforeEach(() => theme(family))

  it('marks each detection Printer or Vigil and shows the printer code and sentence', async () => {
    const { container } = mount()
    await screen.findByText(SENTENCE)
    const markers = [...container.querySelectorAll('[data-source]')]
    expect(markers.map(m => [m.getAttribute('data-source'), m.textContent])).toEqual([['printer', 'Printer'], ['printer', 'Printer'], ['vigil', 'Vigil'], ['vigil', 'Vigil']])
    const reports = container.querySelectorAll('[data-printer-report]')
    expect(reports).toHaveLength(1) // unreadable metadata shows the marker only; Vigil rows show none
    expect(reports[0]).toHaveTextContent(SENTENCE)
    expect(reports[0]).toHaveTextContent('0C00030000030008')
    expect(screen.getAllByText('Informational').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Waste Chute Full').length).toBeGreaterThan(0)
    // The printer gives no score: only Vigil rows carry a confidence badge.
    expect(screen.getByText('82%')).toBeInTheDocument()
    expect(screen.queryByText('100%')).not.toBeInTheDocument()
  })

  it('source filter is sent to the server and resets paging', async () => {
    mount()
    await screen.findByText(SENTENCE)
    const filter = screen.getByRole('combobox', { name: 'Filter by source' })
    expect(within(filter).getAllByRole('option').map(o => o.textContent)).toEqual(['All Sources', 'Printer', 'Vigil'])
    fireEvent.change(filter, { target: { value: 'printer' } })
    await waitFor(() => expect(api.get).toHaveBeenLastCalledWith({ limit: 30, offset: 0, source: 'printer' }))
    fireEvent.change(filter, { target: { value: 'vigil' } })
    await waitFor(() => expect(api.get).toHaveBeenLastCalledWith({ limit: 30, offset: 0, source: 'vigil' }))
    fireEvent.change(filter, { target: { value: '' } })
    await waitFor(() => expect(api.get).toHaveBeenLastCalledWith({ limit: 30, offset: 0 }))
  })

  it('opens from the printer page link already filtered to that printer and source', async () => {
    mount('/detections?source=printer&printer=9')
    await waitFor(() => expect(api.get).toHaveBeenCalledWith({ limit: 30, offset: 0, printer_id: '9', source: 'printer' }))
    expect(screen.getByRole('combobox', { name: 'Filter by source' })).toHaveValue('printer')
    cleanup()
    api.get.mockClear()
    mount('/detections?source=elsewhere&printer=9;drop')
    await waitFor(() => expect(api.get).toHaveBeenCalledWith({ limit: 30, offset: 0 }))
  })

  it('a picture-less printer detection can still be confirmed or dismissed from its details', async () => {
    mount()
    fireEvent.click(await screen.findByText(SENTENCE))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Printer')).toBeInTheDocument()
    expect(within(dialog).getByText(SENTENCE)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm Issue' }))
    await waitFor(() => expect(api.review).toHaveBeenCalledWith(1, 'confirmed'))
  })

  it('phone layout: filters and the printer sentence wrap instead of widening the page', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('max-width'), media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }))
    const { container } = mount()
    await screen.findByText(SENTENCE)
    const filter = screen.getByRole('combobox', { name: 'Filter by source' })
    expect(filter.parentElement).toHaveClass('flex', 'flex-wrap')
    expect(filter.parentElement!.querySelectorAll('select')).toHaveLength(4)
    const report = container.querySelector('[data-printer-report]')!
    expect(report).toHaveClass('break-words')
    expect(report.querySelector('.font-mono')).toHaveClass('whitespace-nowrap')
    // Marker and type share a row that may wrap under the status badge; the grid is one column on a phone.
    expect(container.querySelector('[data-source]')!.parentElement!.parentElement).toHaveClass('flex-wrap')
    expect(container.querySelector('.grid')).toHaveClass('grid-cols-1')
  })
})
