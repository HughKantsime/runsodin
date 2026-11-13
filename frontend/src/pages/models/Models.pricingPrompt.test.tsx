import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Models from './Models'

const state = vi.hoisted(() => ({ pricing: undefined as unknown, allowed: ['settings.edit', 'models.create'] }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: any) => queryKey[0] === 'models'
    ? { data: [{ id: 9, name: 'Synthetic gear', default_filament_type: 'PETG', category: 'Mechanical', is_favorite: false, estimated_cost: 2, suggested_price: 5 }], isLoading: false }
    : queryKey[0] === 'pricing-config' ? { data: state.pricing } : { data: [] },
  useMutation: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => state.allowed.includes(action), canAccessPage: () => true, getCurrentUser: () => ({ id: 1, role: 'admin' }) }))

const mount = () => render(<MemoryRouter initialEntries={['/models']}><Models /></MemoryRouter>)
beforeEach(() => { state.pricing = { configured: false }; state.allowed = ['settings.edit', 'models.create'] })
afterEach(() => cleanup())

test('pricing never saved: whoever may edit pricing is pointed to the Calculator', () => {
  mount()
  expect(screen.getByTestId('pricing-prompt')).toHaveTextContent('Set up pricing to see cost and price on each model.')
  expect(screen.getByRole('link', { name: 'Set up pricing' })).toHaveAttribute('href', '/calculator')
})

test('pricing never saved: a user who may not edit pricing sees no prompt', () => {
  state.allowed = ['models.create']
  mount()
  expect(screen.queryByTestId('pricing-prompt')).toBeNull()
  expect(screen.queryByText(/Set up pricing/)).toBeNull()
})

test('pricing saved: no prompt', () => {
  state.pricing = { configured: true }
  mount()
  expect(screen.queryByTestId('pricing-prompt')).toBeNull()
})

test('an older server that does not say is treated as configured: no prompt', () => {
  state.pricing = { ui_mode: 'advanced' }
  mount()
  expect(screen.queryByTestId('pricing-prompt')).toBeNull()
  state.pricing = undefined
  cleanup(); mount()
  expect(screen.queryByTestId('pricing-prompt')).toBeNull()
})
