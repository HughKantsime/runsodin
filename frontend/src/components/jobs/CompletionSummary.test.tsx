import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import CompletionSummary from './CompletionSummary'
import JobTimeline from './JobTimeline'
import {jobCompletion,CompletionSummary as Summary} from '../../api/jobCompletion'
vi.mock('../../api/jobCompletion',()=>({jobCompletion:{suggestion:vi.fn(),start:vi.fn(),timeline:vi.fn()}}))
vi.mock('../../api/printerMaintenance',()=>({printerMaintenance:{get:vi.fn().mockResolvedValue({state:'ok',note:'',set_by:null,set_by_name:null,set_at:null})}}))
vi.mock('../../permissions',()=>({canDo:()=>true}))
vi.mock('../orders/ProductionBatches',()=>({default:({inspectJobId}:{inspectJobId:number})=><p>Count form for job {inspectJobId}</p>}))
const summary:Summary={job_id:2,job_name:'Mini',printer_id:1,printer:'X1C',actual_print_seconds:3600,recipe_state:'matched',product:['Mini'],expected_outputs:[{product_id:1,product_name:'Mini',kind:'main',expected:5}],health:'history incomplete',health_events:[],inspection_available:true,inspection_unavailable_reason:null,square_projection:[{product_id:1,quantity:10}],bed_line:"ODIN has not checked the bed. Use 'Bed clear, start next' to start."}
const suggestion={job_id:3,reason:'Scheduler queue order',filament:'verify_needed' as const,fingerprint:'a'.repeat(64)}
function mount(element=<CompletionSummary summary={summary}/>){return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>{element}</QueryClientProvider>)}
beforeEach(()=>{vi.resetAllMocks();vi.mocked(jobCompletion.suggestion).mockResolvedValue(suggestion);vi.mocked(jobCompletion.start).mockResolvedValue({success:true,state:'dispatched'})})
afterEach(cleanup)
it('shows durable summary and opens the existing count form for this job',async()=>{
 mount();fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}))
 await screen.findByText(/Next: job 3/)
 expect(screen.getByText('Health: history incomplete')).toBeInTheDocument()
 expect(screen.getByRole('link',{name:'Open inspection'})).toHaveAttribute('href','/products?inspect=2')
 fireEvent.click(screen.getByRole('button',{name:'Record count'}));expect(screen.getByText('Count form for job 2')).toBeInTheDocument()
})
it('requires explicit bed confirmation and sends a UUID and current fingerprint',async()=>{
 mount();fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}));await screen.findByText(/Next: job 3/)
 const button=screen.getByRole('button',{name:'Bed clear, start next'});expect(button).toBeDisabled()
 fireEvent.click(screen.getByLabelText('I checked the printer and cleared the bed.'));fireEvent.click(button)
 await screen.findByText('Next job dispatched.')
 expect(jobCompletion.start).toHaveBeenCalledWith(1,{suggested_job_id:3,suggestion_fingerprint:suggestion.fingerprint,request_id:expect.stringMatching(/^[0-9a-f-]{36}$/),bed_clear_confirmed:true,attestation:'owner_in_app'})
 expect(screen.getByLabelText('I checked the printer and cleared the bed.')).not.toBeChecked()
})
it('network retry reuses request id; a changed suggestion requires fresh confirmation',async()=>{
 vi.mocked(jobCompletion.start).mockRejectedValue(new Error('Network error'))
 mount();fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}));await screen.findByText(/Next: job 3/)
 const confirm=screen.getByLabelText('I checked the printer and cleared the bed.')
 fireEvent.click(confirm);fireEvent.click(screen.getByRole('button',{name:'Bed clear, start next'}));await screen.findByRole('alert')
 await waitFor(()=>expect(confirm).not.toBeChecked());fireEvent.click(confirm);fireEvent.click(screen.getByRole('button',{name:'Bed clear, start next'}))
 await waitFor(()=>expect(jobCompletion.start).toHaveBeenCalledTimes(2))
 expect(vi.mocked(jobCompletion.start).mock.calls[0][1].request_id).toBe(vi.mocked(jobCompletion.start).mock.calls[1][1].request_id)
})
it('unavailable inspection explains why and offers no count form',()=>{
 mount(<CompletionSummary summary={{...summary,inspection_available:false,inspection_unavailable_reason:'Multiple attempts'}}/>);fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}))
 expect(screen.getByText('inspection unavailable: Multiple attempts')).toBeInTheDocument();expect(screen.queryByRole('button',{name:'Record count'})).not.toBeInTheDocument()
})
it('job detail reads and displays the timeline in server order',async()=>{
 vi.mocked(jobCompletion.timeline).mockResolvedValue({events:[{id:'a',kind:'started',code:null,detail:{},occurred_at:'2026-10-25T12:00:00Z'},{id:'b',kind:'runout',code:'SLOT_1',detail:{},occurred_at:'2026-10-25T13:00:00Z'}]})
 mount(<JobTimeline jobId={2}/>);await screen.findByText(/runout · SLOT_1/)
 expect(jobCompletion.timeline).toHaveBeenCalledWith(2);expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('started')
})
it.each(['refused','uncertain'])('%s retries use the appropriate request identity',async(state)=>{
 vi.mocked(jobCompletion.start).mockResolvedValue({success:false,state,message:'Try again'})
 mount();fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}));await screen.findByText(/Next: job 3/)
 fireEvent.click(screen.getByLabelText('I checked the printer and cleared the bed.'))
 const button=screen.getByRole('button',{name:'Bed clear, start next'})
 fireEvent.click(button);await screen.findByRole('alert');await waitFor(()=>expect(button).not.toBeDisabled())
 fireEvent.click(button);await waitFor(()=>expect(jobCompletion.start).toHaveBeenCalledTimes(2))
 const ids=vi.mocked(jobCompletion.start).mock.calls.map(call=>call[1].request_id)
 if(state==='refused')expect(ids[0]).not.toBe(ids[1]);else expect(ids[0]).toBe(ids[1])
})

it('includes the maintenance warning note retained in the completion summary',()=>{
 mount(<CompletionSummary summary={{...summary,maintenance:{state:'warn',note:'Part fan failing'}}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Completion summary · job 2'}))
 expect(screen.getByText('Maintenance warn: Part fan failing')).toBeInTheDocument()
})
