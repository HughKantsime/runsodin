// Cert C (F05-5.1) — real Upload page fed the captured POST /print-files/upload response.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Upload from './Upload'
import fixture from '../../test-fixtures/cert-C-upload-wire.json'
const api = vi.hoisted(() => ({ update: vi.fn() }))
vi.mock('../../api', () => ({
  filaments: { combined: vi.fn().mockResolvedValue([]) }, printFiles: { list: vi.fn().mockResolvedValue([]) }, getApprovalSetting: vi.fn().mockResolvedValue({ require_job_approval: false }),
  models: { update: api.update, schedule: vi.fn(), getVariants: vi.fn(), scheduleOptions: vi.fn(), listWithPricing: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3, role: 'operator' }), canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
class UploadRequest {
  status = 200; responseText = JSON.stringify(fixture.upload); withCredentials = false
  upload = { addEventListener: vi.fn() }; handlers: Record<string, Function> = {}
  addEventListener(name: string, fn: Function) { this.handlers[name] = fn }
  open = vi.fn(); send() { queueMicrotask(() => this.handlers.load?.()) }
}
beforeEach(() => { api.update.mockResolvedValue({}); vi.stubGlobal('XMLHttpRequest', UploadRequest) })
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })
async function upload() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/upload']}><Upload /></MemoryRouter></QueryClientProvider>)
  fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [new File(['cert'], 'Cert bracket.3mf')] } })
  await screen.findByText('Objects on Plate')
}

test('F05-5.1-01..03 real parser output renders metadata, object checklist and an unchecked wipe tower', async () => {
  await upload()
  const boxes = screen.getAllByRole('checkbox')
  expect(boxes.map(b => (b as HTMLInputElement).checked)).toEqual([true, true, true, false])
  expect(screen.getByText('(wipe tower)')).toBeInTheDocument()
  expect(screen.getByText('Sellable pieces:').textContent).toContain('3')
  expect(screen.getAllByText(/Bambu Lab A1/).length).toBeGreaterThan(0)
})

test('F05-5.1-04 toggling an object persists the checked count as quantity_per_bed', async () => {
  await upload()
  fireEvent.click(screen.getAllByRole('checkbox')[0])
  await waitFor(() => expect(api.update).toHaveBeenCalledExactlyOnceWith(fixture.upload.model_id, { quantity_per_bed: 2 }))
})

// Fixed (cert-fix-C): formerly only a checkbox toggle persisted quantity_per_bed, so the default
// checked count shown after upload (3) was never saved and the server stored NULL.
test('F05-5.1-04 default checked count is persisted without user interaction', async () => {
  await upload()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(fixture.upload.model_id, { quantity_per_bed: 3 }), { timeout: 500 })
})

test('F05-5.1-04 unchecking every object is refused visibly instead of sending an invalid 0', async () => {
  await upload()
  const boxes = screen.getAllByRole('checkbox').slice(0, 3)
  boxes.forEach(box => fireEvent.click(box))
  await new Promise(resolve => setTimeout(resolve, 400))
  expect(api.update).not.toHaveBeenCalledWith(fixture.upload.model_id, { quantity_per_bed: 0 })
  expect(api.update).not.toHaveBeenCalled()
})
