import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ReportScheduleManager from './ReportScheduleManager'
const state = vi.hoisted(() => ({ features:['email_notifications','program_reports'], create:vi.fn(), list:vi.fn(), settings:vi.fn() }))
vi.mock('../../api', () => ({orgs:{getSettings:state.settings},reportSchedules:{list:state.list,create:state.create}}))
vi.mock('../../LicenseContext', () => ({useLicense:() => ({hasFeature:(key:string) => state.features.includes(key)})}))
beforeEach(() => {state.settings.mockResolvedValue({timezone:'America/New_York',program_reports_perkins_enabled:false});state.list.mockResolvedValue([]);state.create.mockResolvedValue({id:1});state.features=['email_notifications','program_reports']})
afterEach(() => {cleanup();vi.clearAllMocks()})
function mount() {render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ReportScheduleManager /></QueryClientProvider>)}
test('serializes explicit school and rolling report scope', async () => {
 mount();fireEvent.click(await screen.findByRole('button',{name:'New Schedule'}))
 fireEvent.change(screen.getByLabelText('Report Type'),{target:{value:'program_report'}})
 fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Funding'}})
 fireEvent.change(screen.getByLabelText('Recipients (comma-separated emails)'),{target:{value:'funding@example.test'}})
 fireEvent.change(screen.getByLabelText('School organization ID (required)'),{target:{value:'7'}})
 fireEvent.change(screen.getByLabelText('Term (optional)'),{target:{value:'Fall'}})
 fireEvent.click(screen.getByRole('button',{name:'Create Schedule'}))
 await waitFor(() => expect(state.create).toHaveBeenCalledWith({name:'Funding',report_type:'program_report',frequency:'weekly',recipients:['funding@example.test'],filters:{org_id:7,days:30,term:'Fall',template:'generic'}}))
})
test('hides program reports without their entitlement', async () => {
 state.features=['email_notifications'];mount();fireEvent.click(await screen.findByRole('button',{name:'New Schedule'}))
 expect(screen.queryByRole('option',{name:'program report'})).not.toBeInTheDocument()
})
