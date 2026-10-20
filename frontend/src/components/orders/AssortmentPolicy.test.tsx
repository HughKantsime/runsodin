import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import AssortmentPolicyPanel from './AssortmentPolicy'
import {assortment} from '../../api/assortment'
const identity=vi.hoisted(()=>({role:'admin'}))
vi.mock('../../permissions',()=>({getCurrentUser:()=>identity}))
vi.mock('../../api/assortment',()=>({assortment:{read:vi.fn(),save:vi.fn(),preview:vi.fn()}}))
const state={revision:null,products:[{id:101,name:'Mini',policy:null,rules:{trigger_below:null,minimum_main_batch:null,routine_replenishment:false}}],truncated:false}
const active={revision:'r1',products:[{id:101,name:'Mini',policy:{assortment:'active' as const,kind:'multicolor_character' as const},rules:{trigger_below:5,minimum_main_batch:5,routine_replenishment:true}}],truncated:false}
async function open(){render(<AssortmentPolicyPanel/>);fireEvent.click(screen.getByRole('button',{name:'Show assortment and replenishment'}));await screen.findByRole('option',{name:'Mini · Not configured'});fireEvent.change(screen.getByLabelText('Assortment product'),{target:{value:'101'}})}
beforeEach(()=>{vi.resetAllMocks();identity.role='admin';vi.mocked(assortment.read).mockResolvedValue(state)})
afterEach(cleanup)
it('does nothing while closed',()=>{render(<AssortmentPolicyPanel/>);expect(assortment.read).not.toHaveBeenCalled();expect(assortment.preview).not.toHaveBeenCalled()})
it('requires explicit status/category, never auto-activates product',async()=>{
 await open();expect(screen.getByRole('button',{name:'Save assortment policy'})).toBeDisabled();expect(screen.getByLabelText('Show assortment status')).toHaveValue('');expect(screen.getByLabelText('Production category')).toHaveValue('')
 fireEvent.change(screen.getByLabelText('Show assortment status'),{target:{value:'active'}});fireEvent.change(screen.getByLabelText('Production category'),{target:{value:'multicolor_character'}});expect(screen.getByRole('button',{name:'Check replenishment'})).toBeDisabled();vi.mocked(assortment.save).mockResolvedValue(active);fireEvent.click(screen.getByRole('button',{name:'Save assortment policy'}));await screen.findByText('Assortment policy saved. No stock or jobs changed.');expect(assortment.save).toHaveBeenCalledWith(101,{revision:null,assortment:'active',kind:'multicolor_character'});expect(assortment.preview).not.toHaveBeenCalled()
})
it('shows recorded count and fixed plate rather than deficit, with no start controls',async()=>{
 await open();vi.mocked(assortment.preview).mockResolvedValue({product_id:101,status:'candidate',message:'Below trigger. Fixed-yield options only.',stock:{quantity:'2',state:'known',calculated_at:null},retrieved_at:'2026-10-20T12:00:00Z',advisory_only:true,recipes:[{id:'recipe',name:'Known plate',main_yield:5,printer_id:1,materials:'PLA fixed colors',outputs:[{product_name:'Mini',kind:'main',expected:5}]}]})
 fireEvent.click(screen.getByRole('button',{name:'Check replenishment'}));await screen.findByText('Known plate: one plate produces 5 main pieces');expect(screen.getByText(/Square recorded quantity: 2/)).toBeVisible();expect(screen.queryByRole('button',{name:/start|schedule/i})).not.toBeInTheDocument()
})
it('policy edits clear the previous preview and require save',async()=>{
 await open();vi.mocked(assortment.preview).mockResolvedValue({product_id:101,status:'enough',message:'Enough recorded stock.',stock:null,retrieved_at:null,advisory_only:true,recipes:[]});fireEvent.click(screen.getByRole('button',{name:'Check replenishment'}));await screen.findByText('Enough recorded stock.');fireEvent.change(screen.getByLabelText('Show assortment status'),{target:{value:'inactive'}});expect(screen.queryByText('Enough recorded stock.')).not.toBeInTheDocument();expect(screen.getByRole('button',{name:'Check replenishment'})).toBeDisabled()
})
it('viewer can read classification but cannot edit or check upstream stock',async()=>{
 identity.role='viewer';await open();expect(screen.queryByRole('button',{name:'Check replenishment'})).not.toBeInTheDocument();expect(screen.queryByLabelText('Show assortment status')).not.toBeInTheDocument();expect(assortment.preview).not.toHaveBeenCalled()
})
it('stale save keeps draft without claiming success',async()=>{
 await open();fireEvent.change(screen.getByLabelText('Show assortment status'),{target:{value:'inactive'}});fireEvent.change(screen.getByLabelText('Production category'),{target:{value:'other'}});vi.mocked(assortment.save).mockRejectedValue(new Error('Assortment changed. Reload.'));fireEvent.click(screen.getByRole('button',{name:'Save assortment policy'}));await screen.findByRole('alert');expect(screen.getByLabelText('Show assortment status')).toHaveValue('inactive');expect(screen.queryByText('Assortment policy saved. No stock or jobs changed.')).not.toBeInTheDocument()
})
