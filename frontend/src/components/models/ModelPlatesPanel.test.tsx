import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ variants: vi.fn(), facts: vi.fn(), thumb: vi.fn() }))
vi.mock('../../api/models', () => ({ models: { getVariants: api.variants } }))
vi.mock('../../api/printFilePlates', () => ({ printFilePlates: { facts: api.facts }, fetchPlateThumbnail: api.thumb }))

import ModelPlatesPanel from './ModelPlatesPanel'

const plate = (over = {}) => ({
  index: 1, has_thumbnail: true, print_time_seconds: 5430, weight_grams: 41.37, layer_height: 0.2, warnings: [],
  filaments: [{ slot: 1, type: 'PLA', color: '#FF0000', grams: 29.25 }, { slot: 2, type: 'PETG', color: null, grams: 12.12 }],
  ...over,
})
const facts = (plates = [plate()]) => ({ version: 1, file_hash: 'h', slicer: { name: 'BambuStudio', version: '02.01.00' }, plate_count: plates.length, plates })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ModelPlatesPanel modelId={5} modelName="Bracket" onClose={() => {}} /></QueryClientProvider>)
}

beforeEach(() => {
  api.variants.mockReset().mockResolvedValue({ model_id: 5, model_name: 'Bracket', variants: [{ id: 11, filename: 'a.3mf', printer_model: 'A1' }] })
  api.facts.mockReset().mockResolvedValue(facts())
  api.thumb.mockReset().mockResolvedValue(null)
  URL.createObjectURL = vi.fn(() => 'blob:plate-1')
  URL.revokeObjectURL = vi.fn()
})
afterEach(cleanup)

it('shows a loading state while the files load', () => {
  api.variants.mockReturnValue(new Promise(() => {}))
  mount()
  expect(screen.getByText('Loading plates…')).toBeTruthy()
})

it('shows an empty state for a model without a print file', async () => {
  api.variants.mockResolvedValue({ model_id: 5, model_name: 'Bracket', variants: [] })
  mount()
  expect(await screen.findByText(/no print file/i)).toBeTruthy()
  expect(api.facts).not.toHaveBeenCalled()
})

it('shows not available when the plate route answers 404', async () => {
  api.facts.mockRejectedValue(Object.assign(new Error('nope'), { status: 404 }))
  mount()
  expect(await screen.findByText(/plate details are not available/i)).toBeTruthy()
})

it('shows a retry when analysis is busy (429)', async () => {
  api.facts.mockRejectedValue(Object.assign(new Error('busy'), { status: 429 }))
  mount()
  expect(await screen.findByText(/analysis is busy/i)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
})

it('shows an empty state for a file with no plates', async () => {
  api.facts.mockResolvedValue(facts([]))
  mount()
  expect(await screen.findByText('This file has no plates.')).toBeTruthy()
})

it('lists slicer, plate facts, swatches and grams', async () => {
  mount()
  expect(await screen.findByText('Sliced with BambuStudio 02.01.00 · 1 plate')).toBeTruthy()
  expect(screen.getByText('1h 30m')).toBeTruthy()
  expect(screen.getByText('41.4 g')).toBeTruthy()
  expect(screen.getByText('0.2 mm')).toBeTruthy()
  expect(screen.getByText('29.3 g')).toBeTruthy()
  const swatches = screen.getAllByTestId('filament-swatch')
  expect(swatches).toHaveLength(2)
  expect((swatches[0] as HTMLElement).style.backgroundColor).toBeTruthy()
  expect((swatches[1] as HTMLElement).style.backgroundColor).toBe('')
})

it('tells "none recorded" apart from "not available" for warnings', async () => {
  api.facts.mockResolvedValue(facts([plate({ index: 1, warnings: [] }), plate({ index: 2, warnings: null }),
    plate({ index: 3, warnings: [{ message: 'Floating region', level: 'warning', code: null }] })]))
  mount()
  expect(await screen.findByText('Slicer warnings: none recorded')).toBeTruthy()
  expect(screen.getByText('Slicer warnings: not available')).toBeTruthy()
  expect(screen.getByText('warning: Floating region')).toBeTruthy()
})

it('fetches the picture through the thumbnail route and shows a placeholder without one', async () => {
  api.thumb.mockResolvedValue(new Blob([new Uint8Array([1])], { type: 'image/png' }))
  api.facts.mockResolvedValue(facts([plate({ index: 1 }), plate({ index: 2, has_thumbnail: false })]))
  mount()
  const image = await screen.findByAltText('Plate 1 preview')
  expect(image.getAttribute('src')).toBe('blob:plate-1')
  expect(api.thumb).toHaveBeenCalledTimes(1)
  expect(api.thumb.mock.calls[0][0]).toBe(11)
  expect(api.thumb.mock.calls[0][1]).toBe(1)
  await waitFor(() => expect(screen.getAllByText('No picture')).toHaveLength(1))
})

it('stacks the plate card on a phone and puts it beside the picture from sm up', async () => {
  mount()
  const card = await screen.findByLabelText('Plate 1')
  expect(card.className).toContain('flex-col')
  expect(card.className).toContain('sm:flex-row')
})
