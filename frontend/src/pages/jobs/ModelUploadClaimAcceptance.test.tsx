import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Upload from './Upload'
import Models from '../models/Models'
const api=vi.hoisted(()=>({update:vi.fn(),schedule:vi.fn(),variants:vi.fn(),options:vi.fn()}))
vi.mock('../../api',()=>({
 filaments:{combined:vi.fn().mockResolvedValue([])},printFiles:{list:vi.fn().mockResolvedValue([])},getApprovalSetting:vi.fn().mockResolvedValue({require_job_approval:false}),
 models:{update:api.update,schedule:api.schedule,getVariants:api.variants,scheduleOptions:api.options,listWithPricing:vi.fn().mockResolvedValue([{id:41,name:'Synthetic model',category:'Synthetic',variant_count:1}])},
 printers:{list:vi.fn().mockResolvedValue([{id:9,name:'Synthetic printer',model:'X1C',is_active:true}])},
}))
vi.mock('../../permissions',()=>({getCurrentUser:()=>({id:2,role:'operator'}),canDo:()=>true}))
vi.mock('../../contexts/OrgContext',()=>({useOrg:()=>({orgId:1})}))
const uploadData={model_id:41,is_new_model:true,quantity_per_bed:2,project_name:'Synthetic model',is_sliced:true,filaments:[],objects:[{name:'Synthetic part A',checked:true,is_wipe_tower:false},{name:'Synthetic part B',checked:true,is_wipe_tower:false},{name:'Synthetic wipe tower',checked:false,is_wipe_tower:true}]}
class UploadRequest {
 status=200;responseText=JSON.stringify(uploadData);withCredentials=false
 upload={addEventListener:vi.fn()};handlers:Record<string,Function>={}
 addEventListener(name:string,fn:Function){this.handlers[name]=fn}
 open=vi.fn();send(){queueMicrotask(()=>this.handlers.load?.())}
}
beforeEach(()=>{api.update.mockResolvedValue({});api.schedule.mockResolvedValue({});api.variants.mockResolvedValue({variants:[{id:5,printer_model:'X1C',plate_count:3}]});api.options.mockResolvedValue({print_file_id:5,payload_sha256:'a'.repeat(64),plate_indices:[0,1,2]});vi.stubGlobal('XMLHttpRequest',UploadRequest)})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals()})
function mount(page:React.ReactNode,path='/upload'){
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
 return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{page}</MemoryRouter></QueryClientProvider>)
}
async function upload(){const view=mount(<Upload/>);fireEvent.change(view.container.querySelector('input[type=file]')!,{target:{files:[new File(['synthetic-fixture'], 'synthetic.3mf')]}});await screen.findByText('Objects on Plate');return view}
test('F05-5.1-02 each returned plate object renders a named checklist item',async()=>{
 await upload()
 expect(screen.getByRole('checkbox',{name:'Synthetic part A'})).toBeChecked()
 expect(screen.getByRole('checkbox',{name:'Synthetic part B'})).toBeChecked()
 expect(screen.getByRole('checkbox',{name:/Synthetic wipe tower/})).toBeInTheDocument()
})
test('F05-5.1-03 parser-provided unchecked wipe tower stays unchecked and excluded from count',async()=>{
 await upload()
 expect(screen.getByRole('checkbox',{name:/Synthetic wipe tower/})).not.toBeChecked()
 expect(screen.getByText('Sellable pieces:').textContent).toContain('2')
})
test('F05-5.1-04 object selection changes count and persists quantity_per_bed to exact model',async()=>{
 await upload()
 fireEvent.click(screen.getByRole('checkbox',{name:'Synthetic part A'}))
 expect(screen.getByText('Sellable pieces:').textContent).toContain('1')
 await waitFor(()=>expect(api.update).toHaveBeenCalledExactlyOnceWith(41,{quantity_per_bed:1}))
 // Unchecking every object would send quantity_per_bed=0, which the API rejects (ge=1);
 // the page refuses it visibly instead of firing a doomed PATCH (cert C F05-5.1-04).
 fireEvent.click(screen.getByRole('checkbox',{name:'Synthetic part B'}))
 await new Promise(resolve=>setTimeout(resolve,400))
 expect(api.update).toHaveBeenCalledExactlyOnceWith(41,{quantity_per_bed:1})
})
test('F05-5.1-05 verified schedule options render all selectable plates',async()=>{
 mount(<Models/>,'/models?schedule=41')
 const dialog=await screen.findByRole('dialog')
 expect(await within(dialog).findByRole('button',{name:'Plate 3'})).toBeInTheDocument()
 expect(within(dialog).getByRole('button',{name:'Plate 1'})).toBeInTheDocument()
 expect(within(dialog).getByRole('button',{name:'Plate 2'})).toBeInTheDocument()
 expect(api.variants).toHaveBeenCalledWith(41)
 expect(api.options).toHaveBeenCalledWith(41)
})
test('F05-5.1-05 selected plate index reaches scheduling request',async()=>{
 mount(<Models/>,'/models?schedule=41')
 const dialog=await screen.findByRole('dialog')
 fireEvent.click(await within(dialog).findByRole('button',{name:'Plate 3'}))
 fireEvent.click(await within(dialog).findByRole('button',{name:/Synthetic printer/}))
 fireEvent.click(within(dialog).getByRole('button',{name:'Create Job'}))
 await waitFor(()=>expect(api.schedule).toHaveBeenCalled())
 expect(api.schedule).toHaveBeenCalledWith(41,9,expect.objectContaining({plate_index:2,print_file_id:5,payload_sha256:'a'.repeat(64)}))
})
