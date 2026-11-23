import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import DisplayCurrencySetting from './DisplayCurrencySetting'
import { fetchAPI } from '../../api/client'
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it.each([false,true])('loads and saves the selected scope (global %s)', async global => {
  vi.mocked(fetchAPI).mockResolvedValue({})
  const client = new QueryClient()
  const refresh = vi.spyOn(client, 'invalidateQueries')
  render(<QueryClientProvider client={client}><DisplayCurrencySetting saved="EUR" global={global} basis={global ? 'global' : 'organization'} /></QueryClientProvider>)
  const select = screen.getByLabelText('Display currency')
  expect(select).toHaveValue('EUR')
  fireEvent.change(select, { target: { value: 'JPY' } })
  fireEvent.click(screen.getByRole('button', {name:'Save currency'}))
  await vi.waitFor(() => expect(refresh).toHaveBeenCalledWith({ queryKey: ['config'] }))
  expect(fetchAPI).toHaveBeenCalledWith('/orders/finance-settings' + (global ? '?global_scope=true' : ''), { method:'PUT',body:'{"currency":"JPY"}' })
})
it('shows a refused save', async () => {
  vi.mocked(fetchAPI).mockRejectedValue(new Error('Permission denied'))
  render(<QueryClientProvider client={new QueryClient()}><DisplayCurrencySetting saved={null} global={false} basis={null} /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Display currency'), {target:{value:'GBP'}})
  fireEvent.click(screen.getByRole('button', {name:'Save currency'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Permission denied')
})

it('can pin an inherited currency and then clear its own setting', async () => {
  vi.mocked(fetchAPI).mockResolvedValue({})
  const client = new QueryClient()
  const view = (basis: 'global' | 'organization') => <QueryClientProvider client={client}>
    <DisplayCurrencySetting saved="EUR" global={false} basis={basis} />
  </QueryClientProvider>
  const { rerender } = render(view('global'))
  expect(screen.getByText('Inherited from the installation.')).toBeVisible()
  fireEvent.click(screen.getByRole('button', {name:'Save currency'}))
  await vi.waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/orders/finance-settings', {method:'PUT', body:'{"currency":"EUR"}'}))
  rerender(view('organization'))
  expect(screen.getByText('Set by your organization.')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Display currency'), {target:{value:''}})
  await vi.waitFor(() => expect(screen.getByRole('button', {name:'Save currency'})).toBeEnabled())
  fireEvent.click(screen.getByRole('button', {name:'Save currency'}))
  await vi.waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/orders/finance-settings', {method:'PUT', body:'{"currency":null}'}))
})
