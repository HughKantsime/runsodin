import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import ProductionHistory from './ProductionHistory'
import {productionHistory} from '../../api/production'
vi.mock('../../api/production',()=>({productionHistory:{read:vi.fn(),check:vi.fn(),send:vi.fn()}}))
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
