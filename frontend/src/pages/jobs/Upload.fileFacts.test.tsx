// The upload result says which of the farm's printers the file runs on and what the file states.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Upload from './Upload'
import fixture from '../../test-fixtures/cert-C-upload-wire.json'
vi.mock('../../api', () => ({
  filaments: { combined: vi.fn().mockResolvedValue([]) }, printFiles: { list: vi.fn().mockResolvedValue([]) }, getApprovalSetting: vi.fn().mockResolvedValue({ require_job_approval: false }),
  models: { update: vi.fn(), schedule: vi.fn(), getVariants: vi.fn(), scheduleOptions: vi.fn(), listWithPricing: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3, role: 'operator' }), canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
let response: any = fixture.upload
class UploadRequest {
  status = 200; responseText = JSON.stringify(response); withCredentials = false
  upload = { addEventListener: vi.fn() }; handlers: Record<string, Function> = {}
  addEventListener(name: string, fn: Function) { this.handlers[name] = fn }
  open = vi.fn(); send() { queueMicrotask(() => this.handlers.load?.()) }
}
beforeEach(() => vi.stubGlobal('XMLHttpRequest', UploadRequest))
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); response = fixture.upload })
async function upload(extra: Record<string, unknown>) {
  response = { ...fixture.upload, ...extra }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/upload']}><Upload /></MemoryRouter></QueryClientProvider>)
  fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [new File(['cert'], 'Cert bracket.3mf')] } })
  await screen.findByText('Objects on Plate')
}

test('the upload result gives the count, the names on click, and the file facts', async () => {
  await upload({
    printer_fit: { count: 2, printers: [{ id: 21, name: 'A1-1' }, { id: 29, name: 'A1-2' }], sliced_for: 'Bambu Lab A1' },
    file_facts: { nozzle_diameter: 0.4, layer_height: 0.16, plate_type: 'textured_plate', supports: true, filament_colors: ['#FFFFFF'] },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Runs on 2 of your printers' }))
  expect(within(screen.getByRole('list', { name: 'Printers this file runs on' })).getAllByRole('listitem').map(i => i.textContent)).toEqual(['A1-1', 'A1-2'])
  expect(within(screen.getByRole('list', { name: 'File details' })).getAllByRole('listitem').map(i => i.textContent))
    .toEqual(['0.4 mm nozzle', '0.16 mm layers', 'Textured plate', 'Supports on', 'White'])
})

test('no fitting printer names the printer the file was sliced for', async () => {
  await upload({ printer_fit: { count: 0, printers: [], sliced_for: 'Bambu Lab A1' }, file_facts: null })
  expect(screen.getByText('Runs on none of your printers. It was sliced for Bambu Lab A1.')).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'File details' })).not.toBeInTheDocument()
})

test('an upload result without them shows neither', async () => {
  await upload({ printer_fit: undefined, file_facts: undefined })
  expect(screen.queryByText(/Runs on/)).not.toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'File details' })).not.toBeInTheDocument()
})
