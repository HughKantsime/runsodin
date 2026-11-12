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
  // The list is read again after a group action, so the mock answers as the server would.
  const gone = new Set<number>()
  vi.mocked(api.list).mockImplementation(async () => trio().filter(a => !gone.has(a.id)) as any)
  vi.mocked(api.dismiss).mockImplementation(async (id: number) => { if (id === 2) throw new Error('refused'); gone.add(id) })
  const notify = vi.spyOn(toast, 'error')
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Dismiss 3 alerts' }))
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Failed to dismiss 1 of 3 alerts'))
  expect(api.dismiss).toHaveBeenCalledTimes(3)
  expect(vi.mocked(api.dismiss).mock.calls.map(c => c[0])).toEqual([3, 2, 1])
  await waitFor(() => expect(screen.queryByText('x3')).toBeNull())
  expect(api.list).toHaveBeenCalledTimes(2)   // once on load, once after the action
  expect(screen.getByText(TITLE)).toBeTruthy() // the failed member stays visible
  expect(screen.getAllByRole('button', { name: 'Dismiss' })).toHaveLength(1)
  notify.mockRestore()
})

test('group mark read calls the API only for unread members', async () => {
  const read = new Set<number>([2])
  vi.mocked(api.list).mockImplementation(async () => [mk(3, 12), mk(2, 11), mk(1, 10)].map(a => ({ ...a, is_read: read.has(a.id) })) as any)
  vi.mocked(api.markRead).mockImplementation(async (id: number) => { read.add(id) })
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Mark 2 alerts read' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Mark 2 alerts read' })).toBeNull())
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

test('a group action that finishes after the filter changed reloads the list on screen, not the old one', async () => {
  // Codex sign-off 11-12: rows and the paging offset were patched by hand and went wrong when
  // the filter changed mid-action. Now the current list is simply read again.
  let release: () => void = () => {}
  vi.mocked(api.list).mockResolvedValue(trio() as any)
  vi.mocked(api.dismiss).mockImplementation(() => new Promise<void>(resolve => { release = resolve }) as any)
  show()
  fireEvent.click(await screen.findByRole('button', { name: 'Dismiss 3 alerts' }))
  await waitFor(() => expect(api.dismiss).toHaveBeenCalledTimes(1))
  const before = vi.mocked(api.list).mock.calls.length
  vi.mocked(api.dismiss).mockResolvedValue(undefined as any)
  release()
  await waitFor(() => expect(api.dismiss).toHaveBeenCalledTimes(3))
  await waitFor(() => expect(vi.mocked(api.list).mock.calls.length).toBe(before + 1))
  const last = vi.mocked(api.list).mock.calls.at(-1)![0] as any
  expect(last.offset).toBe(0)
})
