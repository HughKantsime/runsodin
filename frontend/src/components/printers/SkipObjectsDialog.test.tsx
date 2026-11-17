import { StrictMode, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SkipObjectsDialog from './SkipObjectsDialog'
import type { PrintObjects } from '../../types/printer'

let list: PrintObjects
let response: () => Promise<Response>
let transport: ReturnType<typeof vi.fn>
const receipt = { success: true, outcome: 'accepted', sent: ['300'], already_skipped: [], message: 'Skip sent for 1 object. It shows as skipped in the list once the printer confirms.' }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => {
  list = {
    printer_id: 9, supported: true, can_skip: true, experimental: true,
    reason: null, reason_code: null, retry_after_seconds: null, source: 'bambu_3mf',
    print: { print_job_id: 912, job_id: 501, name: 'Parts', plate: 2 },
    skipped_reported: true, current_object_id: null, max_selectable: 1,
    picture: { available: true, aspect: 1.25, front_edge: null },
    objects: [
      { id: '15', number: 1, name: 'Bracket', state: 'skipped', skippable: true, bounds: { x: .1, y: .1, w: .2, h: .2 }, outline: null },
      { id: '300', number: 2, name: 'Bracket', state: 'printing', skippable: true, bounds: { x: .5, y: .2, w: .2, h: .2 }, outline: null },
      { id: '79', number: 3, name: 'Other', state: 'printing', skippable: true, bounds: { x: .2, y: .6, w: .2, h: .2 }, outline: null },
    ],
  }
  response = async () => json(list)
  transport = vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'POST' ? json(receipt) : response())
  vi.stubGlobal('fetch', transport)
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-theme-family') })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: true } } })
  const result = render(<QueryClientProvider client={client}><SkipObjectsDialog printerId={9} onClose={vi.fn()} /></QueryClientProvider>)
  return { ...result, client }
}
async function choose() {
  fireEvent.click(await screen.findByRole('checkbox', { name: '2. Bracket' }))
  fireEvent.click(screen.getByRole('button', { name: 'Review selection' }))
}

it.each(['classic', 'foundry'])('lists numbered objects, skipped state and geometry in %s', async family => {
  document.documentElement.dataset.themeFamily = family
  list.objects[2].skippable = false
  list.skipped_reported = false
  mount()
  expect(await screen.findByText('Parts · Plate 2')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: '1. Bracket' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: '3. Other' })).toBeDisabled()
  expect(screen.getByText(/Cannot be sent to this printer/)).toBeInTheDocument()
  expect(screen.getByText('The printer has not reported any skipped objects for this print.')).toBeInTheDocument()
  expect(screen.getByText(/built from how Bambu's own software sends this command/)).toBeInTheDocument()
  expect(screen.getByTestId('object-picture').parentElement).toHaveAttribute('aria-hidden', 'true')
  expect(screen.queryByText('Front of printer')).not.toBeInTheDocument()
  fireEvent.click(screen.getByTestId('object-picture').querySelectorAll('rect')[1])
  expect(screen.getByRole('checkbox', { name: '2. Bracket' })).toBeChecked()
  expect(screen.getByTestId('object-picture').querySelectorAll('text')[1]).toHaveTextContent(/^2$/)
  expect(screen.getByTestId('object-picture').querySelectorAll('rect')[1]).toHaveAttribute('stroke-width', '1.5')
})

it('omits unavailable pictures and labels the front only when supplied', async () => {
  list.picture.available = false
  const { client } = mount()
  await screen.findByText('Parts · Plate 2')
  expect(screen.queryByTestId('object-picture')).not.toBeInTheDocument()
  list.picture.available = true
  list.picture.front_edge = 'bottom'
  await act(() => client.invalidateQueries({ queryKey: ['print-objects', 9] }))
  expect(await screen.findByText('Front of printer')).toBeInTheDocument()
})

it('caps selection, confirms names and sends exactly one POST with string ids and print id', async () => {
  const { client } = mount()
  fireEvent.click(await screen.findByRole('checkbox', { name: '2. Bracket' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '3. Other' }))
  expect(screen.getByRole('checkbox', { name: '3. Other' })).not.toBeChecked()
  expect(screen.getByRole('status')).toHaveTextContent('To end the print, use Stop.')
  fireEvent.click(screen.getByRole('button', { name: 'Review selection' }))
  expect(screen.getByRole('dialog', { name: 'Skip 1 object?' })).toBeInTheDocument()
  expect(screen.getByText('2. Bracket')).toBeInTheDocument()
  expect(screen.getByText('The printer stops printing this object for the rest of this print. This cannot be undone.')).toBeInTheDocument()
  let finish!: (r: Response) => void
  transport.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const send = screen.getByRole('button', { name: 'Skip 1 object' })
  fireEvent.click(send)
  fireEvent.click(send)
  expect(send).toHaveAttribute('aria-disabled', 'true')
  expect(send).not.toBeDisabled()
  expect(screen.getByRole('button', { name: 'Back' })).toBeDisabled()
  await act(async () => finish(json(receipt)))
  expect(await screen.findByRole('status')).toHaveTextContent(receipt.message)
  expect(transport.mock.calls.filter(([, options]) => options?.method === 'POST')).toEqual([
    ['/api/printers/9/skip-objects', expect.objectContaining({ body: JSON.stringify({ print_job_id: 912, object_ids: ['300'] }) })],
  ])
  expect(transport.mock.calls.filter(([, options]) => options?.method !== 'POST')).toHaveLength(2)
  expect(screen.getByRole('checkbox', { name: '2. Bracket' })).not.toBeChecked()
  expect(client.getQueryData<PrintObjects>(['print-objects', 9])?.objects[1].state).toBe('printing')
})

it.each(['outcome_unknown', 'partly_sent', 'not_connected', 'network'])('requires manual reload and never retries a %s send', async code => {
  mount()
  await choose()
  transport.mockImplementationOnce(async () => {
    if (code === 'network') throw new Error('lost connection')
    return json({ detail: 'Reload before trying again.', error: { code, retriable: false } }, 503)
  })
  fireEvent.click(screen.getByRole('button', { name: 'Skip 1 object' }))
  const reload = await screen.findByRole('button', { name: 'Reload list' })
  expect(screen.queryByRole('button', { name: /Skip \d|Back|Try again|Review selection/ })).not.toBeInTheDocument()
  vi.useFakeTimers()
  await act(() => vi.advanceTimersByTimeAsync(30000))
  expect(transport.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  vi.useRealTimers()
  fireEvent.click(reload)
  expect(await screen.findByRole('checkbox', { name: '2. Bracket' })).not.toBeChecked()
  expect(screen.getByRole('button', { name: 'Review selection' })).toBeDisabled()
})

it('shows loading, then the server refusal and opt-in link without sending', async () => {
  let finish!: (r: Response) => void
  response = () => new Promise(resolve => { finish = resolve })
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('Reading the objects of this print...')
  await act(async () => finish(json({ ...list, supported: false, objects: [], can_skip: false, reason_code: 'experimental_not_enabled', reason: 'Enable Experimental actions first.' })))
  expect(await screen.findByText('Enable Experimental actions first.')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Skip objects settings' })).toHaveAttribute('href', '#experimental-opt-in-9')
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
  expect(transport).toHaveBeenCalledTimes(1)
})

it.each(['file_unavailable', 'not_printing'])('shows the server %s sentence without actions', async reason_code => {
  list = { ...list, supported: false, reason_code, reason: 'The server explains why.', objects: [], can_skip: false }
  mount()
  expect(await screen.findByText('The server explains why.')).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.getAllByRole('button')).toHaveLength(1)
})

it.each([500, 0])('shows read failure and allows explicit reload (status %s)', async status => {
  response = async () => { if (!status) throw new Error('offline'); return json({}, status) }
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('ODIN could not load the object list. Try again.')
  response = async () => json(list)
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByRole('checkbox', { name: '2. Bracket' })).toBeInTheDocument()
})

it('polls only while mounted, honours preparing delay, and discards selection when the print changes or ends', async () => {
  vi.useFakeTimers()
  list = { ...list, supported: false, reason_code: 'preparing', reason: 'Reading file.', retry_after_seconds: 2 }
  const { unmount } = mount()
  await act(() => vi.advanceTimersByTimeAsync(10))
  expect(screen.getByText('Reading file.')).toBeInTheDocument()
  list = { ...list, supported: true, reason_code: null }
  await act(() => vi.advanceTimersByTimeAsync(2010))
  expect(transport).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('checkbox', { name: '2. Bracket' }))
  fireEvent.click(screen.getByRole('button', { name: 'Review selection' }))
  list = { ...list, print: { ...list.print!, print_job_id: 913 } }
  await act(() => vi.advanceTimersByTimeAsync(10010))
  expect(screen.getByRole('checkbox', { name: '2. Bracket' })).not.toBeChecked()
  expect(screen.queryByRole('button', { name: 'Skip 1 object' })).not.toBeInTheDocument()
  list = { ...list, supported: false, reason_code: 'not_printing', reason: 'Nothing is printing on this printer.', print: null, objects: [] }
  await act(() => vi.advanceTimersByTimeAsync(10010))
  expect(screen.getByText('Nothing is printing on this printer.')).toBeInTheDocument()
  unmount()
  const count = transport.mock.calls.length
  await act(() => vi.advanceTimersByTimeAsync(30000))
  expect(transport).toHaveBeenCalledTimes(count)
})

it('honours can_skip even when objects are available', async () => {
  list.can_skip = false
  mount()
  expect(await screen.findByRole('checkbox', { name: '2. Bracket' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Review selection' })).not.toBeInTheDocument()
})

it.each([['classic', false], ['foundry', false], ['classic', true], ['foundry', true]])('moves, traps and restores focus in %s (StrictMode=%s)', async (family, strict) => {
  document.documentElement.dataset.themeFamily = family as string
  function Harness() {
    const [open, setOpen] = useState(false)
    return <><button onClick={() => setOpen(true)}>Open</button>{open && <SkipObjectsDialog printerId={9} onClose={() => setOpen(false)} />}</>
  }
  render(<QueryClientProvider client={new QueryClient()}>{strict ? <StrictMode><Harness /></StrictMode> : <Harness />}</QueryClientProvider>)
  const user = userEvent.setup()
  const opener = screen.getByRole('button', { name: 'Open' })
  await user.click(opener)
  await screen.findByRole('checkbox', { name: '2. Bracket' })
  expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true)
  await user.tab({ shift: true })
  expect(screen.getByRole('checkbox', { name: '3. Other' })).toHaveFocus()
  await user.tab()
  expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(opener).toHaveFocus()
})

it('keeps keyboard focus inside the dialog across confirmation and Back', async () => {
  mount()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('checkbox', { name: '2. Bracket' }))
  await user.click(screen.getByRole('button', { name: 'Review selection' }))
  expect(screen.getByRole('button', { name: 'Back' })).toHaveFocus()
  await user.click(screen.getByRole('button', { name: 'Back' }))
  expect(screen.getByRole('button', { name: 'Review selection' })).toHaveFocus()
})

it('invalidates confirmation when the printer reports a selected object skipped', async () => {
  const { client } = mount()
  await choose()
  list.objects[1].state = 'skipped'
  list.max_selectable = 0
  await act(() => client.invalidateQueries({ queryKey: ['print-objects', 9] }))
  expect(await screen.findByRole('checkbox', { name: '2. Bracket' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Skip 1 object' })).not.toBeInTheDocument()
  expect(transport.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})


it('uses plural confirmation for two objects', async () => {
  list.objects[0].state = 'printing'
  list.max_selectable = 2
  mount()
  fireEvent.click(await screen.findByRole('checkbox', { name: '1. Bracket' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '2. Bracket' }))
  fireEvent.click(screen.getByRole('button', { name: 'Review selection' }))
  expect(screen.getByRole('dialog', { name: 'Skip 2 objects?' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Skip 2 objects' })).toBeInTheDocument()
  expect(screen.getByText('The printer stops printing these objects for the rest of this print. This cannot be undone.')).toBeInTheDocument()
})

it.each(['classic', 'foundry'])('keeps a 44px tick target and number-only picture labels in %s', async family => {
  document.documentElement.dataset.themeFamily = family
  mount()
  const checkbox = await screen.findByRole('checkbox', { name: '2. Bracket' })
  expect(checkbox.closest('label')).not.toBeNull()
  if (family === 'classic') {
    expect(checkbox.closest('label')).toHaveClass('p-2.5')
    expect(checkbox).toHaveClass('h-6', 'w-6') // 24px input + 20px label padding
  } else expect(checkbox.closest('label')).toHaveClass('f-check-target')
  fireEvent.click(checkbox)
  const picture = screen.getByTestId('object-picture')
  expect(Array.from(picture.querySelectorAll('text'), node => node.textContent)).toEqual(['1', '2', '3'])
  expect(picture.querySelector('rect')).toHaveAttribute('stroke-dasharray', '2 1')
})

it('explains the selection cap beside the end of the list', async () => {
  mount()
  fireEvent.click(await screen.findByRole('checkbox', { name: '2. Bracket' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '3. Other' }))
  const status = screen.getByRole('status')
  expect(status).toHaveTextContent('At least one object has to keep printing. To end the print, use Stop.')
  expect(status.previousElementSibling?.tagName).toBe('UL')
  expect(status.nextElementSibling).toHaveTextContent('Review selection')
})

it('brings the selection cap reason into view each time a tick is refused', async () => {
  const scroll = vi.fn()
  const original = Element.prototype.scrollIntoView
  Element.prototype.scrollIntoView = scroll
  try {
    mount()
    fireEvent.click(await screen.findByRole('checkbox', { name: '2. Bracket' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '3. Other' }))
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1))
    expect(scroll.mock.instances[0]).toBe(screen.getByRole('status'))
    expect(scroll).toHaveBeenLastCalledWith({ block: 'nearest' })
    fireEvent.click(screen.getByRole('checkbox', { name: '3. Other' }))
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(2))
  } finally {
    Element.prototype.scrollIntoView = original
  }
})

it('retains focus on send and then focuses the receipt', async () => {
  mount()
  await choose()
  let finish!: (r: Response) => void
  transport.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const user = userEvent.setup()
  const send = screen.getByRole('button', { name: /^Skip 1 object/ })
  await user.click(send)
  expect(send).not.toBeDisabled()
  expect(send).toHaveFocus()
  await user.click(send)
  expect(transport.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  await act(async () => finish(json(receipt)))
  await waitFor(() => expect(screen.getByRole('status')).toHaveFocus())
})

it.each([true, false])('keeps focus after reloading (objects available=%s)', async supported => {
  mount()
  await choose()
  transport.mockImplementationOnce(async () => { throw new Error('lost connection') })
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: /^Skip 1 object/ }))
  const reload = await screen.findByRole('button', { name: 'Reload list' })
  expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true)
  if (!supported) list = { ...list, supported: false, reason_code: 'not_printing', reason: 'This print ended.', objects: [] }
  await user.click(reload)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Reload list' })).not.toBeInTheDocument())
  expect(supported ? screen.getByRole('checkbox', { name: '2. Bracket' }) : screen.getByText('This print ended.')).toHaveFocus()
})
