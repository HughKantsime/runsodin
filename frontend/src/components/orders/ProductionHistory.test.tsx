import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import ProductionHistory from './ProductionHistory'
import {productionHistory} from '../../api/production'
vi.mock('../../api/production',()=>({productionHistory:{read:vi.fn(),check:vi.fn()}}))
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
