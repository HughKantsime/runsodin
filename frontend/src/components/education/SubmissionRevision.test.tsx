import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Upload, { educationAudience } from './SubmissionUploadModal'
import Queue from './SubmissionQueue'
import Workbench from '../../pages/education/EducationWorkbench'
import toast from 'react-hot-toast'
import { refreshPermissions, getCurrentUser } from '../../permissions'
const state = vi.hoisted(() => ({ user: { id: 1, username: 'student', role: 'viewer', group_id: 7 }, upload: vi.fn(), list: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn() } }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isEducation: true, hasFeature: () => true, loading: false, loadError: false }) }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, getEducationMode: () => Promise.resolve({ enabled: true }), education: { capabilities: () => Promise.resolve({ education_enabled: true, student: true, manager: false, tenant_admin: false, student_cost_center_ids: [7, 8], managed_cost_center_ids: [] }), listCenters: () => Promise.resolve({ items: centers.map((c: any) => ({ ...c, counts: { active_grants: 1, active_printers: 0 } })), next_cursor: null }), uploadSubmission: (...a: unknown[]) => state.upload(...a), listSubmissions: (...a: unknown[]) => state.list(...a) } }))
vi.mock('./ReviewDecisionModal', () => ({ default: () => null }))
const centers = [{ id: 7, name: 'Engineering', code: 'ENG', active: true }, { id: 8, name: 'Art', code: 'ART', active: true }] as any
const row = { id: 11, submitted_by: 1, cost_center_id: 7, item_name: 'Original gear', status: 'rejected', rejection_reason: 'Revise supports', created_at: '2026-10-13' } as any
function mount(element: React.ReactElement) { const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], { ...state.user }); const wrap = (child: React.ReactElement) => <QueryClientProvider client={client}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider>; const result = render(wrap(element)); return { ...result, client, update: (child: React.ReactElement) => result.rerender(wrap(child)) } }
function deferred() { let resolve!: (v: any) => void, reject!: (v: Error) => void; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function select(name = 'revised.3mf') { const file = new File(['owned'], name); fireEvent.change(screen.getByLabelText('Sliced Bambu 3MF'), { target: { files: [file] } }); return file }
function submit() { fireEvent.submit(screen.getByRole('button', { name: 'Submit for review' }).closest('form')!) }
beforeEach(() => { state.user = { id: 1, username: 'student', role: 'viewer', group_id: 7 }; vi.mocked(toast.success).mockClear(); state.upload.mockReset(); state.list.mockReset(); state.list.mockResolvedValue({ items: [], next_cursor: null }) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('only offers own rejected active-authorized-center action; history remains visible', async () => {
  state.list.mockResolvedValue({ items: [row, { ...row, id: 12, submitted_by: 2 }, { ...row, id: 13, status: 'submitted' }, { ...row, id: 14, cost_center_id: 99 }], next_cursor: null })
  const revise = vi.fn(); const view = mount(<Queue centers={centers} reviewable={false} mineOnly onRevise={revise} />)
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Revise and resubmit' })).toHaveLength(1)); fireEvent.click(screen.getByRole('button', { name: 'Revise and resubmit' })); expect(revise).toHaveBeenCalledWith(row)
  view.update(<Queue centers={[{ ...centers[0], active: false }]} reviewable={false} mineOnly onRevise={revise} />); expect(screen.queryByRole('button', { name: 'Revise and resubmit' })).not.toBeInTheDocument()
})
it('requires file, preserves File/error/token across refetch+failure, and synchronously suppresses duplicates', async () => {
  const old = deferred(); state.upload.mockReturnValueOnce(old.promise).mockResolvedValue({ id: 22 }); const close = vi.fn(), created = vi.fn()
  const props = { open: true, onClose: close, onCreated: created, centers, revision: row, audience: educationAudience(state.user) }; const view = mount(<Upload {...props} />)
  expect(screen.getByText('Revise supports')).toBeInTheDocument(); expect(screen.getByText(/rejected submission stays/)).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Submit for review' })).toBeDisabled()
  const selected = select(); submit(); submit(); expect(state.upload).toHaveBeenCalledTimes(1); expect(state.upload.mock.calls[0].slice(0, 2)).toEqual([7, selected]); const token = state.upload.mock.calls[0][2]
  await act(async () => old.reject(new Error('Response lost'))); view.update(<Upload {...props} centers={centers.map((c: any) => ({ ...c }))} />)
  expect(screen.getByText('revised.3mf')).toBeInTheDocument(); expect(screen.getByRole('alert')).toHaveTextContent('Response lost'); submit(); await waitFor(() => expect(created).toHaveBeenCalledTimes(1)); expect(state.upload.mock.calls[1][2]).toBe(token); expect(close).toHaveBeenCalledTimes(1)
})
it('new File object and center issue fresh tokens', async () => {
  state.upload.mockRejectedValue(new Error('Retry')); mount(<Upload open onClose={vi.fn()} onCreated={vi.fn()} centers={centers} audience={educationAudience(state.user)} />)
  select(); submit(); await screen.findByRole('alert'); const first = state.upload.mock.calls[0][2]
  select(); submit(); await screen.findByRole('alert'); const second = state.upload.mock.calls[1][2]; expect(second).not.toBe(first)
  fireEvent.change(screen.getByLabelText('Class or club'), { target: { value: '8' } }); submit(); await screen.findByRole('alert'); expect(state.upload.mock.calls[2][2]).not.toBe(second)
})
it.each(['success', 'error'])('old pending %s cannot close/reset/overwrite reopened modal or its pending attempt', async outcome => {
  const old = deferred(), next = deferred(); state.upload.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise); const close = vi.fn(), created = vi.fn(); const props = { onClose: close, onCreated: created, centers, audience: educationAudience(state.user) }
  const view = mount(<Upload {...props} open />); select(); submit(); view.update(<Upload {...props} open={false} />); view.update(<Upload {...props} open />); const selected = select('next.3mf'); submit()
  await act(async () => outcome === 'success' ? old.resolve({ id: 1 }) : old.reject(new Error('Old failure')))
  expect(close).not.toHaveBeenCalled(); expect(created).not.toHaveBeenCalled(); expect(screen.getByText('next.3mf')).toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Submit for review' })).toBeDisabled(); expect(state.upload.mock.calls[1][1]).toBe(selected)
  await act(async () => next.resolve({ id: 2 })); expect(created).toHaveBeenCalledTimes(1)
})
it('rechecks live account before call and ignores stale account completion', async () => {
  const old = deferred(); state.upload.mockReturnValue(old.promise); const close = vi.fn(), created = vi.fn(); const view = mount(<Upload open onClose={close} onCreated={created} centers={centers} audience={educationAudience(state.user)} />)
  select(); state.user = { ...state.user, id: 2 }; view.client.setQueryData(['auth', 'me'], { ...state.user }); submit(); expect(state.upload).not.toHaveBeenCalled(); state.user = { ...state.user, id: 1 }; await act(async () => { view.client.setQueryData(['auth', 'me'], { ...state.user }) }); select(); submit(); expect(state.upload).toHaveBeenCalledTimes(1); state.user = { ...state.user, id: 2 }; view.client.setQueryData(['auth', 'me'], { ...state.user }); await act(async () => old.resolve({ id: 1 })); expect(close).not.toHaveBeenCalled(); expect(created).not.toHaveBeenCalled()
})
it('removed entitlement cannot submit retained file', () => {
  const props = { open: true, onClose: vi.fn(), onCreated: vi.fn(), audience: educationAudience(state.user) }; const view = mount(<Upload {...props} centers={centers} />); select(); view.update(<Upload {...props} centers={[]} />); submit(); expect(state.upload).not.toHaveBeenCalled()
})

it('Workbench opens a fresh revision upload with the original center and reason', async () => {
  state.list.mockResolvedValue({ items: [row], next_cursor: null }); state.upload.mockResolvedValue({ id: 99 })
  mount(<Workbench />); fireEvent.click(await screen.findByRole('button', { name: 'My submissions' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Revise and resubmit' }))
  expect(screen.getByLabelText('Class or club')).toHaveValue('7'); expect(within(screen.getByRole('dialog')).getByText('Revise supports')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Submit for review' })).toBeDisabled(); select(); submit()
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Submit for review' })).not.toBeInTheDocument())
  expect(row.status).toBe('rejected'); expect(row.rejection_reason).toBe('Revise supports')
})
it('Workbench switches audience query keys, closes stale upload and ignores its completion', async () => {
  state.list.mockResolvedValue({ items: [row], next_cursor: null }); const old = deferred(); state.upload.mockReturnValue(old.promise)
  const view = mount(<Workbench />); fireEvent.click(await screen.findByRole('button', { name: 'My submissions' })); fireEvent.click(await screen.findByRole('button', { name: 'Revise and resubmit' })); select(); submit()
  const oldAudience = educationAudience(state.user); state.user = { ...state.user, id: 2, username: 'other' }; vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.includes('/auth/me') ? state.user : {} }))); await refreshPermissions(); expect(getCurrentUser()?.id).toBe(2); await act(async () => { view.client.setQueryData(['auth', 'me'], { ...state.user }) })
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Submit for review' })).not.toBeInTheDocument())
  expect(screen.queryByText('Original gear')).not.toBeInTheDocument()
  await act(async () => old.resolve({ id: 99 })); expect(toast.success).not.toHaveBeenCalled()
  const keys = view.client.getQueryCache().getAll().map(query => query.queryKey)
  expect(keys).toContainEqual(['education-capabilities', oldAudience]); expect(keys).toContainEqual(['education-capabilities', educationAudience(state.user)])
})
