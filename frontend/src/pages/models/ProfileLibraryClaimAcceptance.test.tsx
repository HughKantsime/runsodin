import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Profiles from './Profiles'
import Models from './Models'
const api = vi.hoisted(() => ({ list: vi.fn(), import: vi.fn(), update: vi.fn(), models: vi.fn() }))
vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, profilesApi: { ...actual.profilesApi, list: api.list, import: api.import, update: api.update }, models: { ...actual.models, listWithPricing: api.models }, printers: { list: vi.fn().mockResolvedValue([]) } }
})
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ id: 3, role: 'operator', group_id: 17 }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
const profile = { id: 81, name: 'Fixture print profile', slicer: 'orca', category: 'process', file_format: 'json', tags: 'fast,school', filament_type: 'PETG', created_by: 3 }
const profiles = [profile, { ...profile, id: 82, name: 'Fixture printer profile', category: 'printer' }, { ...profile, id: 83, name: 'Fixture filament profile', category: 'filament' }]
const modelData = [{ id: 1, name: 'Fixture gear', category: 'Mechanical', is_favorite: true, estimated_cost: 2, build_time_hours: 2, total_print_hours: 12, print_count: 6 }, { id: 2, name: 'Fixture ornament', category: 'Decor', is_favorite: false, estimated_cost: 0, build_time_hours: 1, total_print_hours: 3, print_count: 3 }]
const clients: QueryClient[] = []
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>) }
async function library(props: any = {}) { const view = show(<Profiles {...props} />); await screen.findByText(profile.name); return view }
async function models() { show(<Models />); await screen.findByRole('heading', { name: 'Fixture gear' }) }
function stat(label: string) { return screen.getByText(label, { selector: 'span', exact: true }).parentElement!.parentElement! }
beforeEach(() => { vi.clearAllMocks(); api.list.mockResolvedValue({ profiles, total: 51 }); api.import.mockResolvedValue({ imported: 1 }); api.update.mockResolvedValue({ updated: true }); api.models.mockResolvedValue(modelData) })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks() })
it('F05-5.2-03 actual model summary counts models, favorites, categories and costed models', async () => {
  await models(); expect(stat('Models')).toHaveTextContent('2'); expect(stat('Favorites')).toHaveTextContent('1'); expect(stat('Categories')).toHaveTextContent('2'); expect(stat('Costed')).toHaveTextContent('1')
  expect(api.models).toHaveBeenCalledWith(null,17)
})
it.each(['Total Print Hours','Most Printed'])('F05-5.2-03 CLAIM GAP: summary includes advertised %s stat', async label => {
  await models(); expect(screen.getByText(label, { selector: 'span', exact: true })).toBeInTheDocument()
})
it('library totals use authorized measured job statistics rather than model estimates', async () => {
  api.models.mockResolvedValue(modelData.map((model, index) => ({ ...model, job_statistics: { completed_job_count: index ? 2 : 4, actual_print_hours: index ? 1.5 : 3, unknown_timing_job_count: index ? 1 : 0 } })))
  await models()
  expect(stat('Total Print Hours')).toHaveTextContent('4.5h')
  expect(stat('Most Printed')).toHaveTextContent('Fixture gear')
  expect(screen.getByText(/Timing is unknown for 1 completed jobs/)).toBeInTheDocument()
})
it('missing job-statistics permission displays unavailable totals', async () => {
  await models()
  expect(stat('Total Print Hours')).toHaveTextContent('Unavailable')
  expect(stat('Most Printed')).toHaveTextContent('Unavailable')
})
it('unknown timing stays unknown and zero completed jobs have no winner', async () => {
  api.models.mockResolvedValue(modelData.map(model => ({ ...model, job_statistics: { completed_job_count: 0, actual_print_hours: 0, unknown_timing_job_count: 1 } })))
  await models()
  expect(stat('Total Print Hours')).toHaveTextContent('Unknown')
  expect(stat('Most Printed')).toHaveTextContent('None')
})
it('completed-count ties select the same model regardless of response order', async () => {
  api.models.mockResolvedValue([...modelData].reverse().map(model => ({ ...model, job_statistics: { completed_job_count: 2, actual_print_hours: 0, unknown_timing_job_count: 0 } })))
  await models()
  expect(stat('Most Printed')).toHaveTextContent('Fixture gear')
  expect(stat('Total Print Hours')).toHaveTextContent('0.0h')
})
it('F05-5.4-04 uploads an in-memory profile file through Import and offers its actual export destination', async () => {
  const view = await library(); const file = new File(['{"name":"Fixture","type":"process"}'], 'fixture.json', { type: 'application/json' })
  const input = view.container.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).toHaveAttribute('accept','.json,.ini,.cfg,.3mf'); await userEvent.upload(input,file)
  await waitFor(() => expect(api.import).toHaveBeenCalledWith(file)); await waitFor(() => expect(screen.getByRole('button', { name: 'Import', exact: true })).toBeEnabled())
  expect(screen.getAllByTitle('Download')[0]).toHaveAttribute('href','/api/profiles/81/export')
})
it('F05-5.4-04 displays stored profile tags and history affordance', async () => {
  await library(); expect(screen.getAllByText('fast,school')).toHaveLength(3); expect(screen.getAllByTitle('Profile history')).toHaveLength(3)
})
it('F05-5.4-04 CLAIM GAP: operator has a control to edit or add profile tags', async () => {
  await library(); fireEvent.click(screen.getAllByRole('button', { name: /edit tags/i })[0])
  fireEvent.change(screen.getByRole('textbox', { name: 'Tags' }), { target: { value: 'school,reviewed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save tags' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(81, { tags: 'school,reviewed' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
it.each([['Printer','printer'],['Filament','filament'],['Process','process']])('F05-5.4-06 %s category sends its existing category value %s', async (label, category) => {
  await library(); fireEvent.click(screen.getByRole('button', { name: label, exact: true }))
  await waitFor(() => expect(api.list).toHaveBeenCalledWith({ slicer: '', category, search: undefined, page: 1, printer_id: undefined }))
})
it('F05-5.4-07 slicer and text search issue exact filter requests and reset pagination', async () => {
  await library(); fireEvent.click(screen.getByRole('button', { name: 'Next' })); await waitFor(() => expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ page: 2 })))
  fireEvent.click(screen.getByRole('button', { name: 'OrcaSlicer' })); await waitFor(() => expect(api.list).toHaveBeenCalledWith({ slicer: 'orca', category: '', search: undefined, page: 1, printer_id: undefined }))
  fireEvent.change(screen.getByPlaceholderText('Search profiles...'), { target: { value: 'school' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith({ slicer: 'orca', category: '', search: 'school', page: 1, printer_id: undefined }))
})
it('F05-5.4-07 embedded printer association filter sends printer ID, distinct from printer-model filtering', async () => {
  await library({ filterPrinterId: 7, filterSlicer: 'orca' }); expect(api.list).toHaveBeenCalledWith({ slicer: 'orca', category: '', search: undefined, page: 1, printer_id: 7 })
})
it.each(['Printer model','Material'])('F05-5.4-07 CLAIM GAP: standalone profile library has a dedicated %s filter', async label => {
  await library(); expect(screen.getByRole('combobox', { name: new RegExp(label, 'i') })).toBeInTheDocument()
})

it('profile model and material filters use backend fields and reset page', async () => {
  await library(); fireEvent.click(screen.getByRole('button', { name: 'Next' }))
  fireEvent.change(screen.getByRole('combobox', { name: 'Printer model' }), { target: { value: 'Synthetic model' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ page: 1, printer_model: 'Synthetic model' })))
  fireEvent.change(screen.getByRole('combobox', { name: 'Material' }), { target: { value: 'PETG' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ printer_model: 'Synthetic model', filament_type: 'PETG' })))
})

it('real profile adapter serializes model/material filters and tag PUT body', async () => {
  const actual = await import('../../api/models')
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  await actual.profilesApi.list({ printer_model: 'Bambu X1', filament_type: 'PETG', printer_id: 7, page: 1 })
  const url = new URL(String(fetchMock.mock.calls[0][0]), 'https://synthetic.invalid')
  expect(url.pathname).toBe('/api/profiles')
  expect(Object.fromEntries(url.searchParams)).toEqual({ printer_model: 'Bambu X1', filament_type: 'PETG', printer_id: '7', page: '1' })
  await actual.profilesApi.update(81, { tags: 'reviewed,school' })
  expect(fetchMock).toHaveBeenLastCalledWith('/api/profiles/81', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ tags: 'reviewed,school' }) }))
})
