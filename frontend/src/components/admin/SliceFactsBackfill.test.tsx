import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import SliceFactsBackfill from './SliceFactsBackfill'
import { fetchAPI } from '../../api/client'
const actor = vi.hoisted(() => ({ isAdmin: true }))
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => actor }))
const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SliceFactsBackfill /></QueryClientProvider>)
afterEach(() => { cleanup(); vi.resetAllMocks(); actor.isAdmin = true })

it('starts a pass and shows progress without starting a second pass', async () => {
  vi.mocked(fetchAPI).mockResolvedValueOnce({state:'idle'}).mockResolvedValueOnce({state:'running',done:0,total:3,not_available:0})
  mount()
  const button = screen.getByRole('button', {name:'Read slice facts for older files'})
  fireEvent.click(button)
  expect(await screen.findByText(/0 of 3 files checked/)).toBeVisible()
  expect(button).toBeDisabled()
  expect(fetchAPI).toHaveBeenCalledWith('/print-files/plates/backfill', {method:'POST'})
})
it('explains an empty pass', async () => {
  vi.mocked(fetchAPI).mockResolvedValue({state:'complete',total:0,done:0,not_available:0})
  mount()
  expect(await screen.findByText('Every supported file has already been checked.')).toBeVisible()
})
it('keeps install-wide counts private for an organization admin', () => {
  actor.isAdmin = false
  mount()
  expect(screen.queryByRole('button')).toBeNull()
  expect(fetchAPI).not.toHaveBeenCalled()
})
