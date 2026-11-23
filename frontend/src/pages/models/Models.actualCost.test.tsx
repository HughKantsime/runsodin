import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Models from './Models'

const full = { id: 9, name: 'Synthetic gear', jobs: 5, jobs_with_full_cost: 3, jobs_partial: 1, jobs_without_usage: 1, pieces: 4,
  average_actual_per_piece: 5.66, average_estimate_per_piece: 5.25, variance_percent: 7.9, failure_share_percent: 2 }
const state = vi.hoisted(() => ({ pricing: { configured: true } as unknown, licensed: true, rows: [] as unknown[], asked: [] as unknown[] }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: any) => {
    const key = options.queryKey[0]
    if (key === 'cost-per-print') { state.asked.push(options.enabled); return { data: options.enabled ? { configured: true, rows: state.rows } : undefined } }
    return key === 'models'
      ? { data: [{ id: 9, name: 'Synthetic gear', default_filament_type: 'PETG', category: 'Mechanical', is_favorite: false, estimated_cost: 2, suggested_price: 5 }], isLoading: false }
      : key === 'pricing-config' ? { data: state.pricing } : { data: [] }
  },
  useMutation: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: (feature: string) => state.licensed && feature === 'analytics' }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => true, getCurrentUser: () => ({ id: 1, role: 'admin' }) }))

const mount = () => render(<MemoryRouter initialEntries={['/models']}><Models /></MemoryRouter>)
beforeEach(() => { state.pricing = { configured: true }; state.licensed = true; state.rows = [full]; state.asked = [] })
afterEach(() => cleanup())

test('a model with three prints on full cost records shows its actual cost per piece and how it compares', () => {
  mount()
  const line = screen.getByTestId('model-actual-cost')
  expect(line).toHaveTextContent('Actual: 5.66 per piece over 3 prints')
  expect(line).toHaveTextContent('Prints cost 7.9% more than estimated')
})

test('says less than estimated when the prints came in under', () => {
  state.rows = [{ ...full, variance_percent: -24.5 }]
  mount()
  expect(screen.getByTestId('model-actual-cost')).toHaveTextContent('Prints cost 24.5% less than estimated')
})

test('a model with fewer than three such prints shows no actual cost', () => {
  state.rows = [{ ...full, jobs_with_full_cost: 2, average_actual_per_piece: null, average_estimate_per_piece: null, variance_percent: null, failure_share_percent: null }]
  mount()
  expect(screen.queryByTestId('model-actual-cost')).toBeNull()
})

test('below Pro the table is not requested and nothing is shown', () => {
  state.licensed = false
  mount()
  expect(state.asked.every(enabled => enabled === false)).toBe(true)
  expect(screen.queryByTestId('model-actual-cost')).toBeNull()
})

test('until pricing is configured the table is not requested and nothing is shown', () => {
  state.pricing = { configured: false }
  mount()
  expect(state.asked.every(enabled => enabled === false)).toBe(true)
  expect(screen.queryByTestId('model-actual-cost')).toBeNull()
})
