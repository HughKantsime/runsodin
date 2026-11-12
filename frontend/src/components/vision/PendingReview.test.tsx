import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import PendingReview from './PendingReview'

const api = vi.hoisted(() => ({ get: vi.fn(), review: vi.fn(), allowed: { value: true } }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, vision: { getDetections: api.get, reviewDetection: api.review } }))
vi.mock('react-hot-toast', () => ({ default: { error: vi.fn(), success: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: (a: string) => api.allowed.value && a === 'printers.edit' }))
vi.mock('./BoundedVisionImage', () => ({ default: ({ path, alt }: any) => <img src={'/f/' + path} alt={alt} /> }))

const det = (id: number) => ({
  id, printer_id: id, print_job_id: null, detection_type: 'spaghetti', confidence: 0.9, status: 'pending',
  frame_path: `${id}.jpg`, bbox_json: null, metadata_json: null, reviewed_by: null, reviewed_at: null,
  created_at: '2026-10-14T12:00:00', printer_name: `Printer ${id}`,
})

beforeEach(() => {
  api.get.mockReset(); api.review.mockReset(); api.allowed.value = true
  api.get.mockImplementation(async (p: any) => (p.limit === 1 ? { items: [det(1)], total: 3 } : { items: [det(1), det(2), det(3)], total: 3 }))
  api.review.mockResolvedValue({})
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><PendingReview audience="a" current={() => true} /></QueryClientProvider>)
}
async function open() {
  const btn = await screen.findByRole('button', { name: /^Review pending/ })
  btn.focus()
  fireEvent.click(btn)
  await screen.findByRole('dialog', { name: 'Review pending detections' })
  await screen.findByRole('img', { name: /Printer 1/ })
  return btn
}
const key = (k: string, extra: object = {}, target: Element | Document = document.body) => fireEvent.keyDown(target, { key: k, ...extra })

test('F confirms the shown detection once and advances', async () => {
  mount(); await open()
  key('f')
  await screen.findByRole('img', { name: /Printer 2/ })
  expect(api.review).toHaveBeenCalledTimes(1)
  expect(api.review).toHaveBeenCalledWith(1, 'confirmed')
  expect(screen.getByText('1 of 3 reviewed this session, 2 left')).toBeInTheDocument()
})

test('J dismisses the shown detection', async () => {
  mount(); await open()
  key('J')
  await screen.findByRole('img', { name: /Printer 2/ })
  expect(api.review).toHaveBeenCalledWith(1, 'dismissed')
})

test('the visible buttons do the same as the keys', async () => {
  mount(); await open()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Issue' }))
  await screen.findByRole('img', { name: /Printer 2/ })
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
  await screen.findByRole('img', { name: /Printer 3/ })
  expect(api.review.mock.calls).toEqual([[1, 'confirmed'], [2, 'dismissed']])
})

test('key repeat is ignored, and a second press while in flight is ignored', async () => {
  let resolve: (v: unknown) => void = () => {}
  api.review.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  mount(); await open()
  key('f', { repeat: true })
  expect(api.review).not.toHaveBeenCalled()
  key('f')
  key('f')
  key('j')
  expect(api.review).toHaveBeenCalledTimes(1)
  resolve({})
  await screen.findByRole('img', { name: /Printer 2/ })
  expect(api.review).toHaveBeenCalledTimes(1)
})

test('typing in a text field does not label', async () => {
  mount(); await open()
  const input = document.createElement('input')
  document.body.appendChild(input)
  input.focus()
  key('f', {}, input)
  key('j', {}, input)
  expect(api.review).not.toHaveBeenCalled()
  input.remove()
})

test('a failed label stays on the same detection and shows the page error toast', async () => {
  api.review.mockRejectedValueOnce(new Error('boom'))
  mount(); await open()
  key('f')
  await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Review failed: boom'))
  expect(screen.getByRole('img', { name: /Printer 1/ })).toBeInTheDocument()
  expect(screen.getByText('0 of 3 reviewed this session, 3 left')).toBeInTheDocument()
})

test('S and right arrow skip without labelling; left arrow goes back', async () => {
  mount(); await open()
  key('s')
  await screen.findByRole('img', { name: /Printer 2/ })
  key('ArrowRight')
  await screen.findByRole('img', { name: /Printer 3/ })
  key('ArrowLeft')
  await screen.findByRole('img', { name: /Printer 2/ })
  expect(api.review).not.toHaveBeenCalled()
})

test('Escape closes the reviewer and returns focus to the button', async () => {
  mount()
  const btn = await open()
  key('Escape')
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(btn)
})

test('moves into the dialog on open', async () => {
  mount(); await open()
  expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true)
})

test('loads the next page with the offset of the detections still pending', async () => {
  api.get.mockImplementation(async (p: any) => {
    if (p.limit === 1) return { items: [det(1)], total: 3 }
    return p.offset === 0 ? { items: [det(1), det(2)], total: 3 } : { items: [det(3)], total: 1 }
  })
  mount(); await open()
  key('f'); await screen.findByRole('img', { name: /Printer 2/ })
  key('f'); await screen.findByRole('img', { name: /Printer 3/ })
  expect(api.get).toHaveBeenCalledWith({ status: 'pending', limit: 30, offset: 0 })
  expect(api.get).toHaveBeenCalledWith({ status: 'pending', limit: 30, offset: 0 })
  expect(screen.getByText('2 of 3 reviewed this session, 1 left')).toBeInTheDocument()
})

test('no Review pending button without permission', async () => {
  api.allowed.value = false
  mount()
  await new Promise(r => setTimeout(r, 20))
  expect(screen.queryByRole('button', { name: /Review pending/ })).toBeNull()
  expect(api.get).not.toHaveBeenCalled()
})
