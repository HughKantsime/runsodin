import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ProgramReporting from './ProgramReporting'
const api = vi.hoisted(() => ({ options: vi.fn(), get: vi.fn(), download: vi.fn(), records: vi.fn(), mark: vi.fn(), hours: vi.fn(), roster: vi.fn() }))
vi.mock('../../api/educationProgramReports', () => ({ educationProgramReports: api }))
vi.mock('../../api/educationClasses', () => ({ educationClasses: { roster: api.roster } }))
const cls = { id:'c1', title:'Engineering', term:'Fall', cost_center_id:7, org_id:1, state:'active' as const, revision:1, can_manage:true, can_submit:false }
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions:{ queries:{retry:false} } })}><ProgramReporting cls={cls} audience="teacher" /></QueryClientProvider>) }
beforeEach(() => {
  api.options.mockResolvedValue({timezone:'America/New_York',perkins_enabled:true})
  api.get.mockResolvedValue({ notice:'Generic report', currency:null, summary:{jobs:2,cost:null}, basis:{cost:'Job estimate'}, classes:[] })
  api.records.mockResolvedValue({ marks:[], hours:[], can_approve_hours:true })
  api.roster.mockResolvedValue({ items:[{user_id:1,username:'Learner',role:'student'},{user_id:2,username:'Teacher',role:'manager'}] })
  api.mark.mockResolvedValue({revision:1}); api.hours.mockResolvedValue({revision:1}); api.download.mockResolvedValue(undefined)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('generates by program and displays unknown values and basis', async () => {
  mount(); fireEvent.change(screen.getByLabelText('Report scope'), {target:{value:'program'}})
  fireEvent.click(screen.getByRole('button',{name:'Generate report'}))
  expect(await screen.findByText('Generic report')).toBeInTheDocument()
  expect(api.get).toHaveBeenCalledWith(expect.objectContaining({cost_center_id:7,template:'generic'}))
  expect(screen.getByText('Unknown')).toBeInTheDocument(); expect(screen.getByText('cost: Job estimate')).toBeInTheDocument()
})
it('downloads the selected term and draft template with visible warning', async () => {
  mount(); await screen.findByRole('option', {name:'Perkins V draft worksheet'}); fireEvent.change(screen.getByLabelText('Report scope'), {target:{value:'term'}})
  fireEvent.change(screen.getByLabelText('Report template'), {target:{value:'perkins'}})
  expect(screen.getByText(/field definitions not yet confirmed/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Download PDF'}))
  await waitFor(() => expect(api.download).toHaveBeenCalledWith(expect.objectContaining({term:'Fall',template:'perkins'}),'pdf'))
})
it('saves explicit marks with concentrator implying participant', async () => {
  mount(); fireEvent.click(await screen.findByLabelText('Concentrator for Learner'))
  fireEvent.click(screen.getByRole('button',{name:'Save marks for Learner'}))
  await waitFor(() => expect(api.mark).toHaveBeenCalledWith('c1',{user_id:1,revision:0,participant:true,concentrator:true}))
})
it('admin records approved effort with the existing revision', async () => {
  api.records.mockResolvedValue({marks:[],hours:[{instructor_id:2,activity_date:'2026-10-01',hours:1,revision:3}],can_approve_hours:true})
  mount(); fireEvent.change(await screen.findByLabelText('Instructor for teaching hours'),{target:{value:'2'}})
  fireEvent.change(screen.getByLabelText('Instruction date'),{target:{value:'2026-10-01'}})
  fireEvent.change(screen.getByLabelText('Instructor-entered teaching hours'),{target:{value:'2'}})
  await waitFor(() => expect(screen.getByRole('button',{name:'Record teaching hours'})).toBeEnabled())
  fireEvent.click(screen.getByRole('button',{name:'Record teaching hours'}))
  await waitFor(() => expect(api.hours).toHaveBeenCalledWith('c1',{instructor_id:2,activity_date:'2026-10-01',hours:2,revision:3}))
})
it('hides hours approval for instructors and renders server refusals', async () => {
  api.records.mockResolvedValue({marks:[],hours:[],can_approve_hours:false}); api.get.mockRejectedValue(new Error('License expired'))
  mount(); await screen.findByLabelText('Participant for Learner')
  expect(screen.queryByRole('button',{name:'Record teaching hours'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Generate report'})); expect(await screen.findByRole('alert')).toHaveTextContent('License expired')
})

it('reports an existing program without creating a class', async () => {
  const { default: ProgramReportForm } = await import('./ProgramReportForm')
  render(<ProgramReportForm centers={[{id:7,name:'Engineering'}]} />)
  expect(screen.queryByRole('option',{name:'This class'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Generate report'}))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.objectContaining({cost_center_id:7})))
})

it('hides Perkins by default and labels school timezone', async () => {
  api.options.mockResolvedValue({timezone:'America/New_York',perkins_enabled:false})
  mount()
  await screen.findByLabelText('Reporting start (America/New_York)')
  expect(screen.queryByRole('option',{name:'Perkins V draft worksheet'})).not.toBeInTheDocument()
  expect(screen.queryByText(/NEEDS SHANE/)).not.toBeInTheDocument()
})
it('shows orphaned marks and lets a manager clear both flags', async () => {
  api.records.mockResolvedValue({marks:[{user_id:9,participant:true,concentrator:true,revision:2}],hours:[],can_approve_hours:false})
  mount()
  const name = 'Former student #9 (no active grant)'
  fireEvent.click(await screen.findByLabelText(`Participant for ${name}`))
  fireEvent.click(screen.getByRole('button',{name:`Save marks for ${name}`}))
  await waitFor(() => expect(api.mark).toHaveBeenCalledWith('c1',{user_id:9,participant:false,concentrator:false,revision:2}))
})
