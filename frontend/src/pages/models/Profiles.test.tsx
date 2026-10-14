import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { QueryClient,QueryClientProvider } from '@tanstack/react-query'
import { afterEach,beforeEach,expect,test,vi } from 'vitest'
import Profiles from './Profiles'
const state=vi.hoisted(()=>({outcome:{applied:true,commands_sent:2,commands_failed:0,outcome:'applied'} as any,role:'operator',unavailable:false,fileFormat:'json',createdBy:3,apply:vi.fn()}))
vi.mock('../../api',()=>({profilesApi:{list:vi.fn(async()=>{if(state.unavailable)throw new Error('Synthetic profile unavailable');return {profiles:[{id:101,name:'Synthetic temperature',slicer:'klipper',category:'temperature',file_format:state.fileFormat,org_id:1,created_by:state.createdBy}],total:1}}),get:vi.fn(async()=>({id:101,raw_content:'Synthetic current'})),revisions:vi.fn(async()=>[]),apply:(...args:any[])=>state.apply(...args),exportUrl:()=>'/opaque/export',delete:vi.fn(),import:vi.fn()},printers:{list:vi.fn(async()=>[{id:9,name:'Synthetic own',org_id:1,api_type:'moonraker',is_active:true},{id:11,name:'Synthetic shared foreign',org_id:2,shared:true,api_type:'moonraker',is_active:true}])}}))
vi.mock('../../permissions',()=>({canDo:()=>state.role!=='viewer',getCurrentUser:()=>({id:3,role:state.role,group_id:1})}))
vi.mock('react-hot-toast',()=>({default:{success:vi.fn(),error:vi.fn()}}))
import toast from 'react-hot-toast'
function mount(){return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}><Profiles/></QueryClientProvider>)}
beforeEach(()=>{vi.clearAllMocks();state.role='operator';state.unavailable=false;state.fileFormat='json';state.createdBy=3;state.apply.mockImplementation(async()=>state.outcome)})
afterEach(cleanup)
async function apply(){mount();await screen.findByText('Synthetic temperature');fireEvent.click(screen.getByTitle('Apply to printer'));fireEvent.change(screen.getByRole('combobox',{name:'Apply to printer'}),{target:{value:'9'}});fireEvent.click(screen.getAllByRole('button',{name:'Apply',exact:true}).at(-1)!);await waitFor(()=>expect(state.apply).toHaveBeenCalledWith(101,9))}

test.each([{applied:false,outcome:'failed',commands_sent:0,commands_failed:2},{applied:false,outcome:'partial',commands_sent:1,commands_failed:1}])('failed or partial outcome never announces success (%s)',async result=>{
 state.outcome=result;await apply();await waitFor(()=>expect(toast.error).toHaveBeenCalled());expect(toast.success).not.toHaveBeenCalled();expect(screen.getByRole('heading',{name:'Apply "Synthetic temperature"'})).toBeInTheDocument()
})
test('confirmed success reports command count and closes dialog',async()=>{
 state.outcome={applied:true,outcome:'applied',commands_sent:2,commands_failed:0};await apply();await waitFor(()=>expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('2 command(s)')));expect(screen.queryByRole('heading',{name:'Apply "Synthetic temperature"'})).not.toBeInTheDocument()
})
test('shared foreign printer is excluded from command targets',async()=>{
 mount();await screen.findByText('Synthetic temperature');fireEvent.click(screen.getByTitle('Apply to printer'));expect(screen.getByRole('option',{name:'Synthetic own'})).toBeInTheDocument();expect(screen.queryByRole('option',{name:'Synthetic shared foreign'})).not.toBeInTheDocument()
})
test('viewer cannot import delete or send printer commands',async()=>{
 state.role='viewer';mount();await screen.findByText('Synthetic temperature');expect(screen.queryByRole('button',{name:'Import'})).not.toBeInTheDocument();expect(screen.queryByTitle('Delete')).not.toBeInTheDocument();expect(screen.queryByTitle('Apply to printer')).not.toBeInTheDocument()
})

test('unavailable profiles show an error and recover on explicit retry',async()=>{
 state.unavailable=true;mount();await screen.findByText('Unable to load profiles');expect(screen.queryByText('No profiles yet')).not.toBeInTheDocument();state.unavailable=false;fireEvent.click(screen.getByRole('button',{name:'Retry',exact:true}));await screen.findByText('Synthetic temperature');expect(screen.queryByText('Unable to load profiles')).not.toBeInTheDocument()
})


test('native configuration offers import and download controls without printer commands',async()=>{
 state.fileFormat='cfg';mount();await screen.findByText('Synthetic temperature');expect(screen.queryByTitle('Apply to printer')).not.toBeInTheDocument();expect(screen.getByTitle('Download')).toBeInTheDocument();expect(document.querySelector('input[type="file"]')).toHaveAttribute('accept','.json,.ini,.cfg,.3mf');expect(state.apply).not.toHaveBeenCalled()
})


test.each(['viewer','other creator'])('history remains readable without mutation controls for %s',async mode=>{
 if(mode==='viewer')state.role='viewer';else state.createdBy=2;
 mount();await screen.findByText('Synthetic temperature');fireEvent.click(screen.getByTitle('Profile history'));await screen.findByText('No saved revisions yet.',{exact:false});expect(screen.getByLabelText('Current profile content')).toHaveAttribute('readonly');expect(screen.queryByRole('button',{name:'Save revision'})).not.toBeInTheDocument()
})
