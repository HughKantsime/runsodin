import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import VisionSettingsTab from './VisionSettingsTab'
const api=vi.hoisted(()=>({get:vi.fn(),set:vi.fn(),printerGet:vi.fn(),printerSet:vi.fn(),list:vi.fn()}))
vi.mock('../../api',()=>({vision:{getSettings:api.get,updateSettings:api.set,getPrinterSettings:api.printerGet,updatePrinterSettings:api.printerSet},printers:{list:api.list}}))
vi.mock('../../permissions',()=>({getCurrentUser:()=>({id:2,role:'operator',group_id:1})}))
vi.mock('./VisionModelUpload',()=>({default:()=>null}))
beforeEach(()=>{
 api.get.mockResolvedValue({enabled:false,retention_days:45,collect_training_data:0})
 api.set.mockResolvedValue({});api.printerSet.mockResolvedValue({})
 api.list.mockResolvedValue([{id:9,name:'Synthetic camera printer',camera_url:'/synthetic-frame'},{id:10,name:'Synthetic no-camera printer',camera_url:null}])
 api.printerGet.mockResolvedValue({enabled:1,spaghetti_enabled:1,spaghetti_threshold:.65,first_layer_enabled:1,first_layer_threshold:.60,detachment_enabled:1,detachment_threshold:.70,auto_pause:0,collect_training_data:0})
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.restoreAllMocks()})
async function mount(){const view=render(<VisionSettingsTab/>);await screen.findByText('Synthetic camera printer');await screen.findByText('45 days');return view}
function printerCard(){return screen.getByText('Synthetic camera printer').parentElement!.parentElement!}
test('F03-3.3-03 global configuration loads and Save sends changed fields while preserving others',async()=>{
 await mount()
 expect(screen.getByRole('checkbox',{name:'Enable Vigil AI globally'})).not.toBeChecked()
 fireEvent.click(screen.getByRole('checkbox',{name:'Enable Vigil AI globally'}))
 fireEvent.change(screen.getAllByRole('slider')[0],{target:{value:'60'}})
 fireEvent.click(screen.getByRole('button',{name:'Save',exact:true}))
 await waitFor(()=>expect(api.set).toHaveBeenCalledExactlyOnceWith({enabled:true,retention_days:60,collect_training_data:0}))
 expect(await screen.findByText('Settings saved')).toBeInTheDocument()
})
test('F03-3.3-03 failed Save reports failure and enables retry without success',async()=>{
 api.set.mockRejectedValue(new Error('Synthetic rejected'));await mount()
 fireEvent.click(screen.getByRole('button',{name:'Save',exact:true}))
 await screen.findByText('Save failed')
 expect(screen.getByRole('button',{name:'Save',exact:true})).toBeEnabled()
 expect(screen.queryByText('Settings saved')).not.toBeInTheDocument()
})
test('F03-3.3-02 per-printer settings requested only for camera-configured printer',async()=>{
 await mount()
 expect(api.printerGet).toHaveBeenCalledExactlyOnceWith(9)
 expect(screen.queryByText('Synthetic no-camera printer')).not.toBeInTheDocument()
})
test('F03-3.3-02 disabling printer sends numeric flag and hides sensitivity controls after response',async()=>{
 await mount();const card=printerCard()
 fireEvent.click(within(card).getByRole('checkbox',{name:'Enabled',exact:true}))
 await waitFor(()=>expect(api.printerSet).toHaveBeenCalledExactlyOnceWith(9,{enabled:0}))
 await waitFor(()=>expect(within(card).queryByRole('slider')).not.toBeInTheDocument())
 expect(within(card).getByRole('checkbox',{name:'Enabled',exact:true})).not.toBeChecked()
})
test('F03-3.3-02 sensitivity slider sends numeric threshold for correct printer and updates display',async()=>{
 await mount();const card=printerCard()
 fireEvent.change(within(card).getAllByRole('slider')[0],{target:{value:'0.8'}})
 await waitFor(()=>expect(api.printerSet).toHaveBeenCalledExactlyOnceWith(9,{spaghetti_threshold:.8}))
 expect(await within(card).findByText('80%')).toBeInTheDocument()
})
test('F03-3.3-02 rejected printer update leaves server-provided enabled state',async()=>{
 vi.spyOn(console,'error').mockImplementation(()=>{})
 api.printerSet.mockRejectedValue(new Error('Synthetic rejected'));await mount();const card=printerCard()
 fireEvent.click(within(card).getByRole('checkbox',{name:'Enabled',exact:true}))
 await waitFor(()=>expect(api.printerSet).toHaveBeenCalledExactlyOnceWith(9,{enabled:0}))
 expect(within(card).getByRole('checkbox',{name:'Enabled',exact:true})).toBeChecked()
 expect(within(card).getAllByRole('slider')).toHaveLength(3)
})
