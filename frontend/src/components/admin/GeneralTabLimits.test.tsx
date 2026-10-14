import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import GeneralTab from './GeneralTab'
const state = vi.hoisted(() => ({ cap: null as number | null }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'community', isPro: false, max_printers: state.cap, max_users: state.cap, hasFeature: () => false }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal(), pricingConfig: { get: async () => ({}) } }))
afterEach(cleanup)
it.each([null, 0, 2, 374, 9999])('GeneralTab renders cap %s verbatim or Unlimited', cap => {
  state.cap = cap
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['stats'], { printers: { total: 2 } })
  client.setQueryData(['users-count'], [{ id: 1 }])
  render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
  expect(screen.getByText('Printers:').parentElement).toHaveTextContent(`2 / ${cap === null ? 'Unlimited' : cap}`)
  expect(screen.getByText('Users:').parentElement).toHaveTextContent(`1 / ${cap === null ? 'Unlimited' : cap}`)
})
