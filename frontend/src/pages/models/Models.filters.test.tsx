import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Models from './Models'

const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('../../api', () => ({
  models: { listWithPricing: api.list },
  filaments: { combined: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))

beforeEach(() => { api.list.mockReset().mockResolvedValue([]) })
afterEach(cleanup)

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><Models /></MemoryRouter></QueryClientProvider>)
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.queryByText('Loading models...')).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
  return client
}

function range(min: string, max: string) {
  fireEvent.change(screen.getByLabelText('Plates, at least'), { target: { value: min } })
  fireEvent.change(screen.getByLabelText('Plates, at most'), { target: { value: max } })
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
}

test('rejects an inverted plate range before requesting and allows correction', async () => {
  await mount()
  range('5', '2')
  expect(screen.getByRole('alert')).toHaveTextContent('Minimum plates cannot exceed maximum plates.')
  expect(api.list).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('No models match these filters.')).not.toBeInTheDocument()
  range('5', '5')
  await waitFor(() => expect(api.list).toHaveBeenLastCalledWith(null, 1, { plates_min: 5, plates_max: 5 }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(await screen.findByText('No models match these filters.')).toBeInTheDocument()
})

test('clearing an invalid draft resets validation and permits a one-sided range', async () => {
  await mount()
  range('5', '2')
  expect(screen.getByRole('alert')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Plates, at least')).toHaveValue(null)
  expect(screen.getByLabelText('Plates, at most')).toHaveValue(null)
  range('', '2')
  await waitFor(() => expect(api.list).toHaveBeenLastCalledWith(null, 1, { plates_max: 2 }))
})

test.each([422, 500])('shows a filtered request failure (%s), not an empty result, and retries', async status => {
  await mount()
  api.list.mockRejectedValue(Object.assign(new Error('Request failed'), { status }))
  range('2', '5')
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load models: Request failed')
  expect(screen.queryByText('No models match these filters.')).not.toBeInTheDocument()
  expect(screen.queryByText('No models defined yet')).not.toBeInTheDocument()
  api.list.mockResolvedValue([])
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('No models match these filters.')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('shows initial request errors without suggesting the library is empty', async () => {
  api.list.mockRejectedValue(new Error('Service unavailable'))
  await mount()
  expect(screen.getByRole('alert')).toHaveTextContent('Could not load models: Service unavailable')
  expect(screen.queryByText('No models defined yet')).not.toBeInTheDocument()
})

test('does not label a failed refresh of cached empty results as no matches', async () => {
  const client = await mount()
  range('2', '5')
  expect(await screen.findByText('No models match these filters.')).toBeInTheDocument()
  api.list.mockRejectedValue(new Error('Refresh failed'))
  await client.invalidateQueries({ queryKey: ['models'] })
  expect(await screen.findByRole('alert')).toHaveTextContent('Refresh failed')
  expect(screen.queryByText('No models match these filters.')).not.toBeInTheDocument()
})
