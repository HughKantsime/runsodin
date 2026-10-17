import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import SystemNoticesBanner from './SystemNoticesBanner'
// Actual GET /system/notices entry captured by tests/test_contracts/test_trial_license.py::test_trial_wire_capture.
import wire from '../../test-fixtures/trial-license-wire.json'

const api = vi.hoisted(() => ({ list: vi.fn(), dismiss: vi.fn() }))
vi.mock('../../api/system', () => ({ systemNotices: { list: api.list, dismiss: api.dismiss } }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ isAdmin: true }) }))
let notices: unknown[]
beforeEach(() => {
  notices = [wire.trial_ending_notice]
  api.list.mockImplementation(async () => ({ notices }))
  api.dismiss.mockImplementation(async () => { notices = []; return { status: 'dismissed', kind: 'license_trial_ending' } })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function banner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><SystemNoticesBanner /></MemoryRouter></QueryClientProvider>)
}

test('admins see the 3-day trial notice with links to the license tab and pricing', async () => {
  banner()
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Pro trial ends in 3 days')
  expect(alert).toHaveTextContent('your data is kept')
  expect(screen.getByRole('link', { name: 'Open license' })).toHaveAttribute('href', '/settings?tab=license')
  expect(screen.getByRole('link', { name: 'Buy Pro' })).toHaveAttribute('href', 'https://runsodin.com/pricing')
})

test('dismissing the notice tells the server once and hides it', async () => {
  banner()
  fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(api.dismiss).toHaveBeenCalledExactlyOnceWith('license_trial_ending')
})

test('a failed dismissal keeps the notice visible', async () => {
  api.dismiss.mockRejectedValue(new Error('network'))
  banner()
  fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
  await waitFor(() => expect(api.dismiss).toHaveBeenCalled())
  expect(await screen.findByRole('alert')).toHaveTextContent('Pro trial ends in 3 days')
})
