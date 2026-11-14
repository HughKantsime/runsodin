import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import PrintFailureControl from './PrintFailureControl'
import PrintLog from '../../pages/analytics/PrintLog'
import Archives from '../../pages/archives/Archives'
import { fetchAPI } from '../../api/client'
import { canDo, getCurrentUser } from '../../permissions'
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: vi.fn(() => true), getCurrentUser: vi.fn(() => ({ role: 'admin', group_id: 1 })) }))
const correction = { by: 'Operator', at: '2026-11-14T12:00:00Z', reason_label: 'No filament fed / printed air', note: 'Nothing fed.' }
const item = { failure_correction_allowed: true, id: 903, print_job_id: 902, print_name: 'Synthetic stopped print', printer_display: 'Printer', status: 'cancelled', actual_duration_seconds: 3600, filament_used_grams: 50, tags: [], completed_at: '2026-11-14T11:00:00Z' }
function wrap(node: React.ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
}
function show(props = {}) { return wrap(<PrintFailureControl canCorrect printId={902} status="cancelled" endedAt={item.completed_at} {...props} />) }
function open() { fireEvent.click(screen.getByRole('button', { name: 'Mark as failed' })) }
function choose() { fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'no_filament' } }) }
beforeEach(() => { vi.mocked(canDo).mockReturnValue(true); vi.mocked(getCurrentUser).mockReturnValue({ username: 'Admin', role: 'admin', group_id: 1 }); vi.mocked(fetchAPI).mockReset() })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-theme-family') })
test.each(['classic', 'foundry'])('%s: required reason, note and outcome-only save', async family => {
  document.documentElement.dataset.themeFamily = family
  vi.mocked(fetchAPI).mockResolvedValue({ id: 902, status: 'failed' })
  const done = vi.fn(); show({ onCorrected: done }); open()
  expect(screen.getByRole('button', { name: 'Save failure correction' })).toBeDisabled()
  choose(); fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: 'Nothing fed.' } })
  expect(screen.queryByLabelText(/Corrected material use/)).not.toBeInTheDocument()
  expect(screen.queryByLabelText('No material was used')).not.toBeInTheDocument()
  expect(screen.getByText('This changes the outcome only. Recorded filament and cost stay as they are.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Save failure correction' }))
  await waitFor(() => expect(done).toHaveBeenCalledOnce())
  expect(fetchAPI).toHaveBeenCalledWith('/print-jobs/902/mark-failed', { method: 'POST', body: JSON.stringify({ reason: 'no_filament', note: 'Nothing fed.' }) })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
test('refusal retains form and entered note', async () => {
  vi.mocked(fetchAPI).mockRejectedValue(new Error('Resolve the open print confirmation first.'))
  show(); open(); choose()
  fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: 'Printed air.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save failure correction' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('open print confirmation')
  expect(screen.getByLabelText('Note (optional)')).toHaveValue('Printed air.')
  expect(screen.getByLabelText('Note (optional)')).toHaveAttribute('maxlength', '1000')
  expect(fetchAPI).toHaveBeenCalledWith('/print-jobs/902/mark-failed', { method: 'POST', body: JSON.stringify({ reason: 'no_filament', note: 'Printed air.' }) })
})
test('provenance and undo endpoint', async () => {
  vi.mocked(fetchAPI).mockResolvedValue({ id: 902, status: 'cancelled' })
  const done = vi.fn(); show({ status: 'failed', correction, onCorrected: done })
  expect(screen.getByText(/Marked as failed by Operator/)).toHaveTextContent(correction.reason_label)
  fireEvent.click(screen.getByRole('button', { name: 'Back to cancelled' }))
  expect(fetchAPI).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm back to cancelled' }))
  await waitFor(() => expect(done).toHaveBeenCalledOnce())
  expect(fetchAPI).toHaveBeenCalledWith('/print-jobs/902/undo-failure', { method: 'POST' })
})
test('read-only viewer sees provenance without controls', () => {
  vi.mocked(canDo).mockReturnValue(false); show({ status: 'failed', correction })
  expect(screen.getByText(/Marked as failed by/)).toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
test.each(['completed', 'running', 'failed'])('no correction for %s', status => { show({ status }); expect(screen.queryByRole('button')).not.toBeInTheDocument() })
test('unknown end time and missing physical print have no action', () => {
  show({ endedAt: null }); expect(screen.queryByRole('button')).not.toBeInTheDocument()
  cleanup(); show({ printId: undefined }); expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
test('pending submit cannot be duplicated', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(fetchAPI).mockReturnValue(new Promise(resolve => { finish = resolve }))
  show(); open(); choose(); fireEvent.click(screen.getByRole('button', { name: 'Save failure correction' }))
  const saving = screen.getAllByRole('button', { name: 'Saving…' }); saving.forEach(button => fireEvent.click(button))
  expect(fetchAPI).toHaveBeenCalledOnce()
  finish({}); await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
test.each([['classic', true], ['foundry', true], ['classic', false], ['foundry', false]])('%s phone=%s: Print Log form and zero grams', (family, phone) => {
  document.documentElement.dataset.themeFamily = String(family)
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  client.setQueryData(['print-log', 1, '', '', ''], { items: [{ ...item, filament_used_grams: 0 }], total: 1 }); client.setQueryData(['printers-list'], [])
  wrap(<PrintLog />, client); expect(screen.getByText('0.0g')).toBeInTheDocument()
  if (phone) expect(screen.queryByRole('table')).not.toBeInTheDocument()
  open(); const panel = screen.getByRole('dialog', { name: 'Mark as failed' }).firstElementChild!
  if (phone) expect(panel.className).toContain('h-full')
  if (family === 'foundry') expect(panel).toHaveAttribute('data-foundry-ui', 'modal')
})
test.each(['classic', 'foundry'])('%s phone: archive detail has provenance and undo', async family => {
  document.documentElement.dataset.themeFamily = family
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.mocked(fetchAPI).mockImplementation(async (url: string) => url.startsWith('/archives?') ? { items: [{ ...item, status: 'failed', failure_correction: correction }], total: 1 } : [])
  wrap(<Archives />); fireEvent.click(await screen.findByRole('button', { name: /Synthetic stopped print/ }))
  expect(screen.getByText(/Marked as failed by Operator/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Back to cancelled' })).toBeInTheDocument()
})

test('archive form stays in its existing dialog without nested focus traps', async () => {
  vi.mocked(fetchAPI).mockImplementation(async (url: string) => url.startsWith('/archives?') ? { items: [item], total: 1 } : [])
  wrap(<Archives />)
  fireEvent.click(await screen.findByText('Synthetic stopped print')); open()
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(screen.getByLabelText('Reason')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(screen.queryByLabelText('Reason')).not.toBeInTheDocument()
})

test.each([false, true])('undo requires confirmation and can be cancelled, inline=%s', inline => {
  show({ status: 'failed', correction, inline })
  fireEvent.click(screen.getByRole('button', { name: 'Back to cancelled' }))
  expect(fetchAPI).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Confirm back to cancelled' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Keep correction' }))
  expect(fetchAPI).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Confirm back to cancelled' })).not.toBeInTheDocument()
})

test.each(['operator', 'admin', 'custom', 'viewer'])('role %s agrees with backend even when jobs.edit is granted', role => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'User', role, group_id: 1 })
  show()
  expect(screen.queryByRole('button', { name: 'Mark as failed' }) !== null).toBe(['operator', 'admin'].includes(role))
})

test('platform superadmin bypasses the configured jobs.edit matrix', () => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'Admin', role: 'admin', group_id: null })
  vi.mocked(canDo).mockReturnValue(false)
  show(); expect(screen.getByRole('button', { name: 'Mark as failed' })).toBeInTheDocument()
})

test('out-of-tenant history never offers a correction even with jobs.edit', () => {
  show({ canCorrect: false })
  expect(screen.queryByRole('button', { name: 'Mark as failed' })).not.toBeInTheDocument()
})

test('undo displays preserved work before refreshing or closing', async () => {
  vi.mocked(fetchAPI).mockResolvedValue({ status: 'completed', warnings: ['Print 902 has a later outcome (completed); it was left unchanged.'] })
  const done = vi.fn(); show({ status: 'failed', correction, onCorrected: done })
  fireEvent.click(screen.getByRole('button', { name: 'Back to cancelled' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm back to cancelled' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Print 902 has a later outcome (completed)')
  expect(screen.queryByText('The print is back to cancelled.')).not.toBeInTheDocument()
  expect(done).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Done' }))
  await waitFor(() => expect(done).toHaveBeenCalledOnce())
})

test.each(['cancelled', 'failed', 'completed'])('archive retains legacy outcome control for %s when applicable', async status => {
  vi.mocked(fetchAPI).mockImplementation(async (url: string) => url.startsWith('/archives?') ? { items: [{ ...item, job_id: 901, status }], total: 1 } : [])
  wrap(<Archives />); fireEvent.click(await screen.findByText('Synthetic stopped print'))
  const button = screen.queryByRole('button', { name: /Correct outcome to/ })
  expect(button !== null).toBe(status === 'failed')
})

test('legacy outcome error is reset when selecting another archive', async () => {
  const first = { ...item, job_id: 901, status: 'failed' }
  const second = { ...first, id: 904, print_job_id: 905, print_name: 'Second archive' }
  vi.mocked(fetchAPI).mockImplementation(async (url: string) => {
    if (url.startsWith('/archives?')) return { items: [first, second], total: 2 }
    if (url.endsWith('/outcome')) throw new Error('First archive refusal')
    return []
  })
  wrap(<Archives />); fireEvent.click(await screen.findByText('Synthetic stopped print'))
  fireEvent.click(screen.getByRole('button', { name: 'Correct outcome to cancelled' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('First archive refusal')
  fireEvent.click(screen.getByText('Second archive'))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test.each(['cancelled', 'failed'])('server denies a read-only token its %s action', status => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'Admin', role: 'admin', group_id: null })
  show({ status, canCorrect: false, ...(status === 'failed' ? { correction } : {}) })
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
