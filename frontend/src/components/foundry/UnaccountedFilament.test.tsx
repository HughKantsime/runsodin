import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import UnaccountedFilament from './UnaccountedFilament'
const state = vi.hoisted(() => ({ allowed: true, data: undefined as any, isError: false, query: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed }))
vi.mock('@tanstack/react-query', () => ({ useQuery: (options: any) => { state.query(options); return state } }))
beforeEach(() => { state.allowed = true; state.isError = false; state.data = { unresolved: 2, by_cause: [{ title: 'The spool has no known balance' }] }; state.query.mockClear() })
afterEach(cleanup)
function mount() { render(<MemoryRouter><UnaccountedFilament /></MemoryRouter>) }
test('links the count and top cause to filtered archives', () => { mount(); expect(screen.getByRole('link')).toHaveAttribute('href', '/archives?accounting=unresolved'); expect(screen.getByRole('link')).toHaveTextContent('2 finished prints need their filament settled'); expect(screen.getByText(/The spool has no known balance/)).toBeInTheDocument() })
test.each(['denied', 'empty', 'failure', 'loading'])('renders nothing when %s', reason => {
  if (reason === 'denied') state.allowed = false
  if (reason === 'empty') state.data.unresolved = 0
  if (reason === 'failure') state.isError = true
  if (reason === 'loading') state.data = undefined
  mount(); expect(screen.queryByRole('link')).not.toBeInTheDocument()
  if (reason === 'denied') expect(state.query).toHaveBeenCalledWith(expect.objectContaining({ enabled: false }))
})
