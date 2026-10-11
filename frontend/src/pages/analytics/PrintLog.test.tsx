import { render,screen,cleanup,fireEvent,waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,beforeEach,expect,test,vi } from 'vitest'
import {archives} from '../../api'
import toast from 'react-hot-toast'
import PrintLog from './PrintLog'
const state=vi.hoisted(()=>({data:undefined as any,loading:false,error:false,refetch:vi.fn()}))
vi.mock('@tanstack/react-query',()=>({useQuery:({queryKey}:any)=>queryKey[0]==='print-log'?{data:state.data,isLoading:state.loading,isError:state.error,refetch:state.refetch}:{data:[]}}))
const originalCreate=Object.getOwnPropertyDescriptor(URL,'createObjectURL')
const originalRevoke=Object.getOwnPropertyDescriptor(URL,'revokeObjectURL')
const create=vi.fn(()=> 'blob:synthetic');const revoke=vi.fn()
function mount(url='/print-log'){return render(<MemoryRouter initialEntries={[url]}><PrintLog/></MemoryRouter>)}
beforeEach(()=>{state.data=undefined;state.loading=false;state.error=false;state.refetch.mockReset();create.mockClear();revoke.mockClear();Object.defineProperty(URL,'createObjectURL',{configurable:true,value:create});Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:revoke})})
afterEach(()=>{cleanup();vi.restoreAllMocks();if(originalCreate)Object.defineProperty(URL,'createObjectURL',originalCreate);else delete (URL as any).createObjectURL;if(originalRevoke)Object.defineProperty(URL,'revokeObjectURL',originalRevoke);else delete (URL as any).revokeObjectURL})
test('successful empty print log is distinct from unavailable',()=>{
 state.data={items:[],total:0};mount();expect(screen.getByText('No print log entries')).toBeInTheDocument()
})
test('failed print-log request reports unavailable and offers retry',()=>{
 state.error=true;mount();expect(screen.getByRole('alert')).toHaveTextContent('Print log unavailable');expect(screen.queryByText('No print log entries')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Retry'}));expect(state.refetch).toHaveBeenCalledOnce()
})
test('CSV export forwards current filters and preserves actual CSV bytes',async()=>{
 const csv='id,print_name\r\n9,Synthetic print\r\n';const exportRequest=vi.spyOn(archives,'logExport').mockResolvedValue(csv);const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});mount('/print-log?q=Synthetic+print&printer=9&status=completed');fireEvent.click(screen.getByRole('button',{name:'Export CSV'}));await waitFor(()=>expect(click).toHaveBeenCalledOnce());expect(exportRequest).toHaveBeenCalledWith({search:'Synthetic print',printer_id:'9',status:'completed'});const blob=create.mock.calls[0][0] as Blob;expect(blob.type).toBe('text/csv');const content=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result as string);reader.onerror=reject;reader.readAsText(blob)});expect(content).toBe(csv);expect(revoke).toHaveBeenCalledWith('blob:synthetic')
})
test('failed export shows recoverable feedback and creates no file',async()=>{
 vi.spyOn(archives,'logExport').mockRejectedValue(new Error('Synthetic unavailable'));const feedback=vi.spyOn(toast,'error');mount();fireEvent.click(screen.getByRole('button',{name:'Export CSV'}));await waitFor(()=>expect(feedback).toHaveBeenCalledWith('Export failed: Synthetic unavailable'));expect(create).not.toHaveBeenCalled()
})
test('pending export disables duplicate requests and restores control on failure',async()=>{
 let reject!:(reason:Error)=>void;const exportRequest=vi.spyOn(archives,'logExport').mockImplementation(()=>new Promise((_resolve,r)=>{reject=r}));vi.spyOn(toast,'error');mount();const button=screen.getByRole('button',{name:'Export CSV'});fireEvent.click(button);expect(button).toBeDisabled();fireEvent.click(button);expect(exportRequest).toHaveBeenCalledOnce();reject(new Error('Synthetic pending failed'));await waitFor(()=>expect(button).not.toBeDisabled());expect(create).not.toHaveBeenCalled()
})
test('object URL is released when browser download setup throws',async()=>{
 vi.spyOn(archives,'logExport').mockResolvedValue('id\r\n9\r\n');vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{throw new Error('Synthetic browser unavailable')});vi.spyOn(toast,'error');mount();fireEvent.click(screen.getByRole('button',{name:'Export CSV'}));await waitFor(()=>expect(revoke).toHaveBeenCalledWith('blob:synthetic'))
})
