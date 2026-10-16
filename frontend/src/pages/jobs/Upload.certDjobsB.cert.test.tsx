// cert-D jobsB: Upload success screen against the producer wire captured by
// tests/test_contracts/test_cert_D_jobsB_api.py (real upload + real POST /models/{id}/schedule).
// Claims F04-4.2-07 (Quick Print) and F04-4.2-06 (Upload -> Library -> Schedule handoff).
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Upload from './Upload'
import wire from '../../test-fixtures/certD-jobsB-wire.json'

const api = vi.hoisted(() => ({ schedule: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('../../api', () => ({
  models: { schedule: api.schedule },
  printFiles: { list: vi.fn().mockResolvedValue([]) },
  getApprovalSetting: vi.fn().mockResolvedValue({ require_job_approval: false }),
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3, role: 'operator' }), canDo: () => true }))
vi.mock('react-hot-toast', () => ({ default: { success: api.success, error: api.error } }))

const uploadBody = { ...wire.quick_print_upload, project_name: 'Synthetic quick cube' }
class UploadRequest {
  status = 200; responseText = JSON.stringify(uploadBody); withCredentials = false
  upload = { addEventListener: vi.fn() }; handlers: Record<string, () => void> = {}
  addEventListener(name: string, fn: () => void) { this.handlers[name] = fn }
  open = vi.fn(); send() { queueMicrotask(() => this.handlers.load?.()) }
}
function Where() { const l = useLocation(); return <output data-testid="where">{l.pathname + l.search}</output> }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/upload']}>
    <Routes><Route path="*" element={<><Where /><Upload /></>} /></Routes></MemoryRouter></QueryClientProvider>)
  fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [new File(['x'], 'Synthetic quick cube.3mf')] } })
  return view
}
beforeEach(() => { vi.stubGlobal('XMLHttpRequest', UploadRequest) })
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

describe('F04-4.2-07 Quick Print on upload success (producer wire)', () => {
  it('click queues exactly the uploaded model with no options and locks against duplicates', async () => {
    api.schedule.mockResolvedValue({ ...wire.quick_print_schedule, job_id: 101, model_name: 'Synthetic quick cube' })
    mount()
    const button = await screen.findByRole('button', { name: 'Quick Print' })
    expect(button).toHaveAttribute('title', 'Auto-assign to an available printer and queue immediately')
    fireEvent.click(button)
    await waitFor(() => expect(api.schedule).toHaveBeenCalledTimes(1))
    expect(api.schedule.mock.calls[0]).toEqual([wire.quick_print_upload.model_id])
    const queued = await screen.findByRole('button', { name: 'Queued!' })
    expect(queued).toBeDisabled()
    expect(api.success).toHaveBeenCalledWith('Job queued')
    fireEvent.click(queued)
    expect(api.schedule).toHaveBeenCalledTimes(1)
    expect(wire.quick_print_schedule.status).toBe('pending')
  })

  it('in-flight request disables the button so a double click queues one job', async () => {
    let resolve: (v: unknown) => void = () => {}
    api.schedule.mockImplementation(() => new Promise(r => { resolve = r }))
    mount()
    const button = await screen.findByRole('button', { name: 'Quick Print' })
    fireEvent.click(button)
    expect(await screen.findByRole('button', { name: 'Queuing...' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Queuing...' }))
    resolve({ ...wire.quick_print_schedule, job_id: 101 })
    expect(await screen.findByRole('button', { name: 'Queued!' })).toBeDisabled()
    expect(api.schedule).toHaveBeenCalledTimes(1)
  })
})

describe('F04-4.2-06 upload success hands off to Model Library and Schedule', () => {
  it('View in Library opens the library and Schedule Now opens the uploaded model schedule', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /View in Library/ }))
    expect(screen.getByTestId('where')).toHaveTextContent('/models')
    cleanup()
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /Schedule Now/ }))
    expect(screen.getByTestId('where')).toHaveTextContent(`/models?schedule=${wire.quick_print_upload.model_id}`)
  })
})
