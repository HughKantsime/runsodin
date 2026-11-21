import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import ProductionHistory from './ProductionHistory'
import {production, productionHistory} from '../../api/production'
vi.mock('../../api/production',()=>({production:{state:vi.fn(),inspect:vi.fn(),recipe:vi.fn()},productionHistory:{read:vi.fn(),check:vi.fn(),send:vi.fn()}}))
const receipt={id:'a'.repeat(32),recipe_id:'recipe',job_id:2,recipe_name:'Test minis',occurred_at:'2026-10-20T12:00:00Z',stock_status:'prepared',outputs:[{product_id:101,product_name:'Mini',kind:'main' as const,expected:5,accepted:4,rejected:1}]}
const addition={id:'addition1',receipt_id:receipt.id,state:'unknown' as const,attempts:1}
const page={receipts:[receipt],additions:[addition],next_offset:null}
async function open(operator=true){render(<ProductionHistory operator={operator}/>);fireEvent.click(screen.getByRole('button',{name:'Inspection history and Square recovery'}));await screen.findByText('Test minis · job 2')}
beforeEach(()=>{vi.resetAllMocks();vi.mocked(productionHistory.read).mockResolvedValue(page)})
afterEach(cleanup)
it('stays quiet while closed',()=>{render(<ProductionHistory operator/>);expect(productionHistory.read).not.toHaveBeenCalled();expect(productionHistory.check).not.toHaveBeenCalled()})
it('reconciles exact history and removes action after confirmation',async()=>{
 await open();expect(productionHistory.check).not.toHaveBeenCalled();vi.mocked(productionHistory.check).mockResolvedValue({id:addition.id,state:'confirmed',attempts:1,readiness:'confirmed',message:'Exact history matches. No stock was sent.',square_writes_enabled:false,retry_allowed:false})
 fireEvent.click(screen.getByRole('button',{name:'Check Square history'}));await screen.findByText('Exact history matches. No stock was sent.');expect(screen.queryByRole('button',{name:'Check Square history'})).not.toBeInTheDocument();expect(screen.getByText(/Square: Confirmed/)).toBeVisible()
})
it('blocked check retains uncertain status and shows actionable reason',async()=>{
 await open();vi.mocked(productionHistory.check).mockResolvedValue({id:addition.id,state:'unknown',attempts:1,readiness:'blocked',message:'Square account changed. Do not retarget.',square_writes_enabled:false,retry_allowed:false});fireEvent.click(screen.getByRole('button',{name:'Check Square history'}));await screen.findByText('Square account changed. Do not retarget.');expect(screen.getByText(/Square: Unknown outcome/)).toBeVisible();expect(screen.queryByRole('button',{name:/send|retry/i})).not.toBeInTheDocument()
})
it('search and paging retain applied criteria while form edited',async()=>{
 await open();vi.mocked(productionHistory.read).mockResolvedValue({...page,next_offset:25});fireEvent.change(screen.getByLabelText('Search inspections'),{target:{value:'mini'}});fireEvent.change(screen.getByLabelText('Square addition status'),{target:{value:'unknown'}});fireEvent.click(screen.getByRole('button',{name:'Search or reload history'}));await screen.findByRole('button',{name:'Load older inspections'});fireEvent.change(screen.getByLabelText('Search inspections'),{target:{value:'unsaved'}});vi.mocked(productionHistory.read).mockResolvedValue({receipts:[],additions:[],next_offset:null});fireEvent.click(screen.getByRole('button',{name:'Load older inspections'}));await waitFor(()=>expect(productionHistory.read).toHaveBeenLastCalledWith(25,'mini','unknown'))
})
it('viewer reads without external-check controls',async()=>{await open(false);expect(screen.queryByRole('button',{name:'Check Square history'})).not.toBeInTheDocument();expect(productionHistory.check).not.toHaveBeenCalled()})
it('network error is not a completion claim',async()=>{await open();vi.mocked(productionHistory.check).mockRejectedValue(new Error('Read unavailable'));fireEvent.click(screen.getByRole('button',{name:'Check Square history'}));await screen.findByRole('alert');expect(screen.getByText(/Square: Unknown outcome/)).toBeVisible()})

it('parent refresh reloads open history without polling or resetting search',async()=>{
 const rendered=render(<ProductionHistory operator refreshKey={0}/>);fireEvent.click(screen.getByRole('button',{name:'Inspection history and Square recovery'}));await screen.findByText('Test minis · job 2');expect(productionHistory.read).toHaveBeenCalledTimes(1)
 rendered.rerender(<ProductionHistory operator refreshKey={1}/>);await waitFor(()=>expect(productionHistory.read).toHaveBeenCalledTimes(2));expect(productionHistory.check).not.toHaveBeenCalled()
})


it('hides Send while either activation key is off',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'prepared',attempts:0}],square_writes_enabled:false})
 await open();expect(screen.queryByRole('button',{name:/^Send$/})).not.toBeInTheDocument()
})
it('hides Send for viewers even when activated',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'prepared',attempts:0}],square_writes_enabled:true})
 await open(false);expect(screen.queryByRole('button',{name:/^Send$/})).not.toBeInTheDocument()
})
it('checks then confirms product quantity location and current count before one send',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'prepared',attempts:0}],square_writes_enabled:true})
 vi.mocked(productionHistory.check).mockResolvedValue({id:addition.id,state:'prepared',attempts:0,readiness:'checks_passed',message:'Checks passed',square_writes_enabled:true,retry_allowed:false,preview:[{product:'Mini',quantity:'4',location:'Shop',location_id:'L1',current_count:'300'}]})
 vi.mocked(productionHistory.send).mockResolvedValue({id:addition.id,state:'confirmed',attempts:1,readiness:'confirmed',message:'Square acknowledged this addition.',square_writes_enabled:false,retry_allowed:false})
 await open();fireEvent.click(screen.getByRole('button',{name:/^Send$/}));await screen.findByRole('dialog',{name:'Confirm Square stock addition'})
 expect(screen.getByText('Mini: send 4 to Shop (L1). Current Square count: 300.')).toBeVisible();expect(productionHistory.send).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Confirm send'}));await screen.findByText('Square acknowledged this addition.');expect(productionHistory.send).toHaveBeenCalledExactlyOnceWith(addition.id);expect(screen.queryByRole('button',{name:/^Send$/})).not.toBeInTheDocument()
})
it('cancel closes confirmation without sending',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'prepared',attempts:0}],square_writes_enabled:true})
 vi.mocked(productionHistory.check).mockResolvedValue({id:addition.id,state:'prepared',attempts:0,readiness:'checks_passed',message:'Checks passed',square_writes_enabled:true,retry_allowed:false,preview:[{product:'Mini',quantity:'4',location:'Shop',location_id:'L1',current_count:'300'}]})
 await open();fireEvent.click(screen.getByRole('button',{name:/^Send$/}));await screen.findByRole('dialog');fireEvent.click(screen.getByRole('button',{name:'Cancel'}));expect(productionHistory.send).not.toHaveBeenCalled();expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('blocked preflight displays the reason and opens no confirmation',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'prepared',attempts:0}],square_writes_enabled:true})
 vi.mocked(productionHistory.check).mockResolvedValue({id:addition.id,state:'prepared',attempts:0,readiness:'blocked',message:'baseline_review',square_writes_enabled:false,retry_allowed:false})
 await open();fireEvent.click(screen.getByRole('button',{name:/^Send$/}));await screen.findByText('baseline_review');expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(productionHistory.send).not.toHaveBeenCalled()
})
it('withdrawn and baseline-only receipts remain visible without send controls',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,additions:[{...addition,state:'withdrawn_baseline',attempts:0}],square_writes_enabled:true})
 await open();expect(screen.getByText(/Square: Withdrawn into baseline/)).toBeVisible();expect(screen.queryByRole('button',{name:/^Send$/})).not.toBeInTheDocument();expect(productionHistory.check).not.toHaveBeenCalled()
})

it('shows each frozen mixed-output disposition',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,receipts:[{...receipt,outputs:[{...receipt.outputs[0],disposition:'in_baseline',send_quantity:0},{...receipt.outputs[0],kind:'flush',accepted:1,rejected:1,disposition:'send',send_quantity:1}]}]})
 await open();expect(screen.getByText(/Mini \(main\).*Counted in baseline/)).toBeVisible();expect(screen.getByText(/Mini \(flush\).*1 to send/)).toBeVisible()
})
it('labels an outside-print receipt with its physical ID and attested source',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({...page,receipts:[{...receipt,job_id:undefined,print_job_id:120}]})
 render(<ProductionHistory operator/>);fireEvent.click(screen.getByRole('button',{name:'Inspection history and Square recovery'}))
 await screen.findByText('Test minis · Owner-attested outside print 120')
 expect(screen.queryByText(/job undefined/)).not.toBeInTheDocument()
})

it.each([true,false])('records only missed pieces with confirmation (create revision %s)',async(create)=>{
 const base={id:receipt.recipe_id,name:'Mini plate',qualified:true,reference_job_id:1,materials:'PLA',outputs:receipt.outputs}
 const flush={product_id:102,kind:'flush' as const,expected:2,product_name:'Charm'}
 const revision={...base,id:'revised',extends_recipe_id:base.id,outputs:[...base.outputs,flush]}
 vi.mocked(production.state).mockResolvedValue({recipes:create?[base]:[base,revision],receipts:[],additions:[],next_offset:null,square_writes_enabled:false})
 vi.mocked(productionHistory.read).mockResolvedValue({...page,receipts:[{...receipt,counted_by:'Avery',note:'First tray'}]})
 render(<ProductionHistory operator admin={create} products={[{id:102,name:'Charm'}]}/>)
 fireEvent.click(screen.getByRole('button',{name:'Inspection history and Square recovery'}))
 await screen.findByText('Counted by Avery')
 expect(screen.getByText('Note: First tray')).toBeVisible()
 fireEvent.click(screen.getByRole('button',{name:'Add pieces missed at inspection'}))
 if(create){
   fireEvent.change(await screen.findByLabelText('Product'),{target:{value:'102'}})
   fireEvent.change(screen.getByLabelText('Expected pieces'),{target:{value:'2'}})
   fireEvent.click(screen.getByLabelText(/I verified the added pieces/))
   vi.mocked(production.recipe).mockResolvedValue(revision)
   fireEvent.click(screen.getByRole('button',{name:'Save extended recipe'}))
   await waitFor(()=>expect(production.recipe).toHaveBeenCalledWith(expect.objectContaining({extends_recipe_id:base.id,reference_job_id:1,
     outputs:[{product_id:101,kind:'main',expected:5},{product_id:102,kind:'flush',expected:2}]})))
 }
 fireEvent.change(await screen.findByLabelText('Accepted pieces'),{target:{value:'0'}})
 fireEvent.change(screen.getByLabelText('Rejected pieces'),{target:{value:'2'}})
 fireEvent.change(screen.getByLabelText('Counted by'),{target:{value:'Jo'}})
 fireEvent.change(screen.getByLabelText('Note'),{target:{value:'Second tray'}})
 const submit=screen.getByRole('button',{name:'Record additional pieces'})
 fireEvent.click(submit)
 expect(production.inspect).not.toHaveBeenCalled()
 fireEvent.click(screen.getByLabelText(/I inspected these pieces/))
 fireEvent.click(submit)
 await waitFor(()=>expect(production.inspect).toHaveBeenCalledWith({recipe_id:revision.id,job_id:2,supplement_of:receipt.id,inspection_confirmed:true,
   not_already_added_confirmed:true,counted_by:'Jo',note:'Second tray',square_revision:null,outputs:[{product_id:102,kind:'flush',accepted:0,rejected:2}]}))
 expect(productionHistory.send).not.toHaveBeenCalled()
})

it('hides missed-piece entry for recipes without a dispatched reference and jobs already supplemented',async()=>{
 for (const flag of [{can_supplement:false},{has_supplement:true}]) {
   vi.mocked(productionHistory.read).mockResolvedValue({...page,receipts:[{...receipt,...flag}]})
   await open()
   expect(screen.queryByRole('button',{name:'Add pieces missed at inspection'})).not.toBeInTheDocument()
   cleanup()
 }
})

it('shows a component search result with its count and note',async()=>{
 vi.mocked(productionHistory.read).mockResolvedValue({receipts:[{
   ...receipt,id:'component1',recipe_id:null,recipe_name:'Handle model',stock_status:'components_only',outputs:[],
   unit_description:'Handle',accepted:3,rejected:1,counted_by:'Alex',note:'Shelf C'
 }],additions:[],next_offset:null})
 render(<ProductionHistory operator/>)
 fireEvent.click(screen.getByRole('button',{name:'Inspection history and Square recovery'}))
 await screen.findByText('Handle model · job 2')
 expect(screen.getByText('Component inspection · Handle: 3 accepted, 1 rejected')).toBeVisible()
 expect(screen.getByText('Counted by Alex')).toBeVisible()
 expect(screen.getByText('Note: Shelf C')).toBeVisible()
 expect(screen.queryByText(/No linked addition/)).not.toBeInTheDocument()
})
