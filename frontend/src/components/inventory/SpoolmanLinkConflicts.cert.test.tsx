// Cert C (F06-6.6-01 review) — admin warning for duplicate Spoolman links, fed the real endpoint shape.
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import SpoolmanLinkConflicts from './SpoolmanLinkConflicts'

const api = vi.hoisted(() => ({ linkConflicts: vi.fn() }))
vi.mock('../../api', () => ({ spools: { linkConflicts: api.linkConflicts } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><SpoolmanLinkConflicts /></QueryClientProvider>)
}

it('names every conflicting spool for an admin', async () => {
  api.linkConflicts.mockResolvedValue([{ spoolman_spool_id: 77, spools: [{ id: 3, qr_code: 'SPL-AAAA0001', org_id: 1, label: '#3 (SPL-AAAA0001)' }, { id: null, qr_code: null, org_id: null, label: 'a spool in another organization' }] }])
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('Spoolman spool 77: #3 (SPL-AAAA0001), a spool in another organization')
})

it('renders nothing without conflicts or for a non-admin (403)', async () => {
  api.linkConflicts.mockResolvedValueOnce([])
  const first = mount()
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(first.container).toBeEmptyDOMElement()
  cleanup()
  api.linkConflicts.mockRejectedValueOnce(new Error('403'))
  const second = mount()
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(second.container).toBeEmptyDOMElement()
})
