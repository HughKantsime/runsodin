import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Upload from '../../pages/jobs/Upload'
import GeneralTab from '../admin/GeneralTab'
const api=vi.hoisted(()=>({schedule:vi.fn(),approvalGet:vi.fn(),approvalSet:vi.fn(),approval:false,error:vi.fn()}))
vi.mock('../../api',()=>({models:{schedule:api.schedule},printFiles:{list:vi.fn().mockResolvedValue([])},getApprovalSetting:api.approvalGet,setApprovalSetting:api.approvalSet,getEducationMode:vi.fn().mockResolvedValue({enabled:false}),setEducationMode:vi.fn(),config:{get:vi.fn().mockResolvedValue({}),getNetwork:vi.fn().mockResolvedValue({})},pricingConfig:{get:vi.fn().mockResolvedValue({ui_mode:'advanced'})},fetchAPI:vi.fn().mockResolvedValue({}),users:{list:vi.fn().mockResolvedValue([])}}))
vi.mock('../../permissions',()=>({getCurrentUser:()=>({id:2,role:'admin'})}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({isPro:true,hasFeature:(feature:string)=>feature==='job_approval'})}))
vi.mock('react-hot-toast',()=>({default:{success:vi.fn(),error:api.error}}))
class UploadRequest {
 status=200;responseText=JSON.stringify({model_id:41,project_name:'Synthetic quick print',is_sliced:true,filaments:[]});withCredentials=false
 upload={addEventListener:vi.fn()};handlers:Record<string,Function>={}
 addEventListener(name:string,fn:Function){this.handlers[name]=fn}
 open=vi.fn();send(){queueMicrotask(()=>this.handlers.load?.())}
}
beforeEach(()=>{api.approval=false;api.approvalGet.mockImplementation(async()=>({require_job_approval:api.approval}));api.approvalSet.mockImplementation(async value=>{api.approval=value;return {require_job_approval:value}});api.schedule.mockResolvedValue({status:'pending'});vi.stubGlobal('XMLHttpRequest',UploadRequest)})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals()})
function mount(child:React.ReactNode){const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});return render(<QueryClientProvider client={client}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider>)}
async function general(){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});await client.fetchQuery({queryKey:['approval-setting'],queryFn:api.approvalGet});render(<QueryClientProvider client={client}><GeneralTab/></QueryClientProvider>)}
async function uploaded(){const view=mount(<Upload/>);fireEvent.change(view.container.querySelector('input[type=file]')!,{target:{files:[new File(['synthetic'],'synthetic.3mf')]}});return await screen.findByRole('button',{name:'Quick Print'})}
test('F04-4.2-07 upload Quick Print schedules exact model and disables already-queued duplicate',async()=>{
 const button=await uploaded();fireEvent.click(button)
 await waitFor(()=>expect(api.schedule).toHaveBeenCalledExactlyOnceWith(41))
 expect(await screen.findByRole('button',{name:'Queued!'})).toBeDisabled()
 fireEvent.click(screen.getByRole('button',{name:'Queued!'}));expect(api.schedule).toHaveBeenCalledOnce()
})
test('F04-4.2-07 failed Quick Print exposes feedback and restores retry without Queued claim',async()=>{
 api.schedule.mockRejectedValue(new Error('Synthetic queue failure'));fireEvent.click(await uploaded())
 await waitFor(()=>expect(api.error).toHaveBeenCalledWith('Quick print failed: Synthetic queue failure'))
 expect(screen.getByRole('button',{name:'Quick Print'})).toBeEnabled();expect(screen.queryByRole('button',{name:'Queued!'})).not.toBeInTheDocument()
})
test('F04-4.6-01 approval setting switch loads disabled and persists enable/disable with query refresh',async()=>{
 await general();await screen.findByText('Approval disabled — all users create jobs directly')
 const toggle=screen.getByText(/^Approval (disabled|required)/).closest('label')!.querySelector('[role=switch]')!;expect(toggle).toHaveAttribute('aria-checked','false');fireEvent.click(toggle)
 await waitFor(()=>expect(api.approvalSet).toHaveBeenCalledWith(true))
 await screen.findByText('Approval required for viewer-role users');expect(toggle).toHaveAttribute('aria-checked','true')
 fireEvent.click(toggle);await waitFor(()=>expect(api.approvalSet).toHaveBeenLastCalledWith(false));await screen.findByText('Approval disabled — all users create jobs directly')
})
test('F04-4.6-01 rejected approval update retains disabled state and reports feedback',async()=>{
 api.approvalSet.mockRejectedValue(new Error('Synthetic rejected'));await general();await screen.findByText('Approval disabled — all users create jobs directly')
 fireEvent.click(screen.getByText(/^Approval (disabled|required)/).closest('label')!.querySelector('[role=switch]')!);await waitFor(()=>expect(api.error).toHaveBeenCalledWith('Toggle approval failed: Synthetic rejected'))
 expect(screen.getByText(/^Approval (disabled|required)/).closest('label')!.querySelector('[role=switch]')!).toHaveAttribute('aria-checked','false')
})
