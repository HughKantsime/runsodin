import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import AuditLogs from './AuditLogs'
import { MemoryRouter } from 'react-router-dom'

const api = vi.hoisted(() => ({ list: vi.fn(), exportCsv: vi.fn(), error: vi.fn() }))
vi.mock('../../api', () => ({ auditLogs: { list: api.list, exportCsv: api.exportCsv }, auth: { me: vi.fn() } }))
vi.mock('react-hot-toast', () => ({ default: { error: api.error } }))
const row = { id: 1, timestamp: '2026-10-13T12:00:00', username: null, action: 'create', entity_type: 'job', entity_id: 0, details: null, ip_address: null }
function show(principal = { id: 3, role: 'admin', group_id: null }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  client.setQueryData(['auth', 'me'], principal)
  render(<QueryClientProvider client={client}><MemoryRouter><AuditLogs /></MemoryRouter></QueryClientProvider>)
  return client
}
beforeEach(() => {
  vi.clearAllMocks()
  api.list.mockResolvedValue({ logs: [row], total: 1, limit: 50, offset: 0 })
  api.exportCsv.mockResolvedValue('ID,Action\n1,create\n')
  class OwnedURL extends URL {
    static createObjectURL = vi.fn(() => 'blob:owned-audit')
    static revokeObjectURL = vi.fn()
  }
  vi.stubGlobal('URL', OwnedURL)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('server search shows a match outside the initially loaded page', async () => {
  api.list.mockImplementation(async params => params.q ? { logs: [{ ...row, id: 999, action: 'old target' }], total: 1 } : { logs: [row], total: 60 })
  show()
  await screen.findByText('create')
  fireEvent.change(screen.getByPlaceholderText('Search logs...'), { target: { value: 'old target' } })
  await screen.findByText('old target')
  expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'old target', offset: 0 }))
  expect(screen.queryByText('create')).not.toBeInTheDocument()
})

test('unknown actor is explicit and entity zero remains visible', async () => {
  show()
  await screen.findByText('create')
  expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0)
  expect(screen.getByText('0')).toBeVisible()
})

test('failed history fetch is visible and Retry refetches', async () => {
  api.list.mockRejectedValueOnce(new Error('Owned refusal')).mockResolvedValue({ logs: [row], total: 1 })
  show()
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await screen.findByText('create')
  expect(api.list).toHaveBeenCalledTimes(2)
})

test('export denial never creates a download', async () => {
  api.exportCsv.mockRejectedValueOnce(new Error('Owned 403'))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  show()
  await screen.findByText('create')
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
  await waitFor(() => expect(api.error).toHaveBeenCalled())
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(click).not.toHaveBeenCalled()
})

test('export sends all visible filters and rejects a duplicate click', async () => {
  let resolve!: (value: string) => void
  api.exportCsv.mockReturnValue(new Promise<string>(r => { resolve = r }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  show()
  await screen.findByText('create')
  fireEvent.change(screen.getByPlaceholderText('Search logs...'), { target: { value: 'owned_%' } })
  const button = screen.getByRole('button', { name: 'Export CSV' })
  fireEvent.click(button); fireEvent.click(button)
  expect(api.exportCsv).toHaveBeenCalledTimes(1)
  expect(api.exportCsv).toHaveBeenCalledWith(expect.objectContaining({ q: 'owned_%' }))
  await act(async () => resolve('ID,Action\n1,create\n'))
  expect(click).toHaveBeenCalledTimes(1)
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:owned-audit')
})

test('account switch cancels an outstanding CSV download', async () => {
  let resolve!: (value: string) => void
  api.exportCsv.mockReturnValue(new Promise<string>(r => { resolve = r }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const client = show()
  await screen.findByText('create')
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
  await act(async () => { client.setQueryData(['auth', 'me'], { id: 4, role: 'viewer', group_id: 1 }) })
  await screen.findByRole('alert')
  await act(async () => resolve('ID,Action\n1,create\n'))
  expect(click).not.toHaveBeenCalled()
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(screen.queryByText('create')).not.toBeInTheDocument()
})


test('recorded human actor and service key remain distinct from historical unknown', async () => {
  api.list.mockResolvedValue({ logs: [
    { ...row, id: 1, username: 'Recorded human', user_id: 3, auth_kind: 'bearer_jwt' },
    { ...row, id: 2, username: null, auth_kind: 'legacy_global_api_key' },
    { ...row, id: 3, username: null, auth_kind: null },
  ], total: 3 })
  show()
  expect(await screen.findByText('Recorded human')).toBeVisible()
  expect(screen.getByText('Service API key')).toBeVisible()
  expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0)
})


test.each([0, false])('preserves recorded scalar details %s and discloses the CSV limit', async details => {
  api.list.mockResolvedValue({ logs: [{ ...row, details }], total: 1 })
  show()
  await screen.findByText('create')
  expect(screen.getByRole('button', { name: JSON.stringify(details) })).toBeVisible()
  expect(screen.getByText('CSV export includes up to 5,000 matching entries.')).toBeVisible()
})
