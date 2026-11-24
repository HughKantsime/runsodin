import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { config } from '../api/system'
import { CurrencyProvider, useMoney } from './DisplayCurrency'

vi.mock('../api/system', () => ({ config: { get: vi.fn() } }))
afterEach(cleanup)
function Amount() {
  const money = useMoney()
  return <><output>{money('12.50')}</output><output>{money(3.25)}</output></>
}
it('refreshes displayed amounts when the server setting is saved or cleared', async () => {
  vi.mocked(config.get).mockResolvedValue({ currency: 'CAD' })
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><CurrencyProvider><Amount /></CurrencyProvider></QueryClientProvider>)
  expect(await screen.findByText(/CAD\s+12.50/)).toBeVisible()
  expect(screen.getByText('CAD 3.25')).toBeVisible()
  vi.mocked(config.get).mockResolvedValue({ currency: null })
  await client.invalidateQueries({ queryKey: ['config'] })
  expect(await screen.findByText('12.50')).toBeVisible()
  expect(screen.getByText('3.25')).toBeVisible()
})
