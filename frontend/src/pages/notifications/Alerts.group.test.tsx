import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import Alerts from './Alerts'
import { alerts as api } from '../../api'
import { getCurrentUser } from '../../permissions'

vi.mock('../../api', () => ({ alerts: { list: vi.fn(), remove: vi.fn(), markRead: vi.fn(), markAllRead: vi.fn(), dismiss: vi.fn() }, fetchAPI: vi.fn(async () => ({ user_digest: null })), approveJob: vi.fn(), rejectJob: vi.fn(), printers: {} }))
vi.mock('../../permissions', () => ({ getCurrentUser: vi.fn() }))

const TITLE = 'Hubble: Pikachu Magnet (H2D) 12-up 5-colour auto-flush paused'
function mk(id: number, hour: number, over: Record<string, unknown> = {}) {
  return { id, title: TITLE, message: null, severity: 'warning', alert_type: 'print_paused', is_read: false, is_dismissed: false, printer_id: 7, printer_name: 'H2D-1', created_at: `2026-10-11T${String(hour).padStart(2, '0')}:00:00`, ...over }
}
const trio = () => [mk(3, 12), mk(2, 11), mk(1, 10)]

function show() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Alerts /></MemoryRouter></QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'op', role: 'operator' } as any)
  vi.mocked(api.dismiss).mockResolvedValue(undefined as any)
  vi.mocked(api.markRead).mockResolvedValue(undefined as any)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

test('a group shows the count and expands to its members', async () => {
  vi.mocked(api.list).mockResolvedValue(trio() as any)
  show()
  await screen.findByText('x3')
  expect(screen.getAllByText(TITLE)).toHaveLength(1)
  expect(screen.getByText(TITLE).getAttribute('title')).toBe(TITLE)
  expect(screen.getByText(/^first .*, last /)).toBeTruthy()
  expect(screen.getByText('3 unread')).toBeTruthy() // counts stay per alert
  const toggle = screen.getByRole('button', { name: 'Show 3 alerts' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(screen.queryAllByRole('button', { name: 'Dismiss' })).toHaveLength(0)
  fireEvent.click(toggle)
  expect(screen.getByRole('button', { name: 'Hide 3 alerts' }).getAttribute('aria-expanded')).toBe('true')
  expect(screen.getAllByRole('button', { name: 'Dismiss' })).toHaveLength(3)
  expect(screen.getAllByRole('button', { name: 'Mark read' })).toHaveLength(3)
})

test('group dismiss calls the API once per member and keeps a failed member', async () => {
  vi.mocked(api.list).mockResolvedValue(trio() as any)
  vi.mocked(api.dismiss).mockImplementation(async (id: number) => { if (id === 2) throw new Error('refused') })
  const notify = vi.spyOn(toast, 'error')
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Dismiss 3 alerts' }))
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Failed to dismiss 1 of 3 alerts'))
  expect(api.dismiss).toHaveBeenCalledTimes(3)
  expect(vi.mocked(api.dismiss).mock.calls.map(c => c[0])).toEqual([3, 2, 1])
  expect(screen.queryByText('x3')).toBeNull()
  expect(screen.getByText(TITLE)).toBeTruthy() // the failed member stays visible
  expect(screen.getAllByRole('button', { name: 'Dismiss' })).toHaveLength(1)
  notify.mockRestore()
})

test('group mark read calls the API only for unread members', async () => {
  vi.mocked(api.list).mockResolvedValue([mk(3, 12), mk(2, 11, { is_read: true }), mk(1, 10)] as any)
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 alerts read' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Mark 3 alerts read' })).toBeNull())
  expect(vi.mocked(api.markRead).mock.calls.map(c => c[0])).toEqual([3, 1])
})

test('a single alert renders as before', async () => {
  vi.mocked(api.list).mockResolvedValue([mk(1, 10)] as any)
  show()
  const title = await screen.findByText(TITLE)
  expect(screen.queryByText(/^x\d+$/)).toBeNull()
  expect(document.querySelector('[aria-expanded]')).toBeNull()
  expect(title.className).not.toContain('line-clamp')
  expect(title.getAttribute('title')).toBeNull()
  expect(screen.getByRole('button', { name: 'Mark read' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Delete alert' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Dismiss' })).toBeTruthy()
})
