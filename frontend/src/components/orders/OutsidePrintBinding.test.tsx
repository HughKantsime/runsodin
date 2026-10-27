import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import OutsidePrintBinding from './OutsidePrintBinding'
import {production} from '../../api/production'
import {printJobs} from '../../api/jobs'
vi.mock('../../api/jobs',()=>({printJobs:{list:vi.fn()}}))
vi.mock('../../api/production',()=>({production:{qualifyOutside:vi.fn(),bindOutside:vi.fn()}}))
const onBound=vi.fn()
const recipe={id:'a'.repeat(32),name:'Existing plate',qualified:true,materials:'PLA',reference_job_id:1,outputs:[]}
const binding={id:'binding',recipe_id:'b'.repeat(32),print_job_id:120,odin_dispatched:false as const,owner_attested:true as const}
beforeEach(()=>{vi.resetAllMocks();onBound.mockResolvedValue(undefined);vi.mocked(production.qualifyOutside).mockResolvedValue(binding);vi.mocked(production.bindOutside).mockResolvedValue(binding)})
afterEach(cleanup)
function mount(admin=true){render(<OutsidePrintBinding recipes={[recipe]} products={[{id:101,name:'Latch'}]} admin={admin} onBound={onBound}/>);fireEvent.click(screen.getByRole('button',{name:'Bind an outside print'}))}
function fill(){
 mount();fireEvent.change(screen.getByLabelText('Outside print ID'),{target:{value:'120'}})
 fireEvent.click(screen.getByLabelText('Qualify a new recipe from this print'))
 fireEvent.change(screen.getByLabelText('Observed printer ID'),{target:{value:'301'}})
 fireEvent.change(screen.getByLabelText('Outside recipe name'),{target:{value:'First plate'}})
 fireEvent.change(screen.getByLabelText('Outside recipe material and colors'),{target:{value:'PLA black'}})
 fireEvent.change(screen.getByLabelText('Outside output 1 product'),{target:{value:'101'}})
 fireEvent.change(screen.getByLabelText('Outside output 1 expected pieces'),{target:{value:'150'}})
}
function attest(){
 fireEvent.click(screen.getByLabelText('I verified the material setup and fixed yield of this outside print.'))
 fireEvent.click(screen.getByLabelText('I confirm this print was started outside ODIN.'))
 fireEvent.click(screen.getByLabelText('I confirm the exact file revision and printed plate.'))
}
it('qualifies and binds only after physical, file and yield confirmation',async()=>{
 fill();const submit=screen.getByRole('button',{name:'Qualify recipe and bind print'});expect(submit).toBeDisabled()
 attest();expect(submit).toBeEnabled();fireEvent.click(submit)
 await waitFor(()=>expect(production.qualifyOutside).toHaveBeenCalledWith(120,{printer_id:301,plate_index:0,name:'First plate',materials:'PLA black',qualification_confirmed:true,outputs:[{product_id:101,kind:'main',expected:150}],attestation:{started_outside_odin:true,file_revision_confirmed:true}}))
 await screen.findByText(/Outside print 120 bound and recipe qualified/)
 expect(onBound).toHaveBeenCalledOnce();expect(submit).toBeDisabled();expect(production.bindOutside).not.toHaveBeenCalled()
})
it('keeps input on ambiguity, then sends the explicit file and plate',async()=>{
 fill();attest();vi.mocked(production.qualifyOutside).mockRejectedValueOnce(new Error('Choose an explicit print_file_id'))
 fireEvent.click(screen.getByRole('button',{name:'Qualify recipe and bind print'}));await screen.findByRole('alert')
 expect(screen.getByLabelText('Outside recipe name')).toHaveValue('First plate');expect(onBound).not.toHaveBeenCalled()
 fireEvent.change(screen.getByLabelText('Print file ID (optional for a unique filename match)'),{target:{value:'501'}})
 fireEvent.change(screen.getByLabelText('Printed plate number'),{target:{value:'2'}})
 expect(screen.getByRole('button',{name:'Qualify recipe and bind print'})).toBeDisabled();attest()
 fireEvent.click(screen.getByRole('button',{name:'Qualify recipe and bind print'}));await waitFor(()=>expect(production.qualifyOutside).toHaveBeenLastCalledWith(120,expect.objectContaining({print_file_id:501,plate_index:1})))
 await screen.findByText(/Outside print 120 bound and recipe qualified/)
})
it('operators can bind an existing recipe but cannot qualify a new one',async()=>{
 mount(false);expect(screen.queryByLabelText('Qualify a new recipe from this print')).not.toBeInTheDocument()
 fireEvent.change(screen.getByLabelText('Outside print ID'),{target:{value:'120'}})
 fireEvent.change(screen.getByLabelText('Existing qualified recipe'),{target:{value:recipe.id}})
 fireEvent.click(screen.getByLabelText('I confirm this print was started outside ODIN.'));fireEvent.click(screen.getByLabelText('I confirm the exact file revision and printed plate.'))
 fireEvent.click(screen.getByRole('button',{name:'Bind completed outside print'}))
 await waitFor(()=>expect(production.bindOutside).toHaveBeenCalledWith(120,{recipe_id:recipe.id,attestation:{started_outside_odin:true,file_revision_confirmed:true}}))
 await screen.findByText(/Outside print 120 bound/);expect(production.qualifyOutside).not.toHaveBeenCalled()
})

it('loads completed physical observations and selects their printer without using archive IDs',async()=>{
 mount()
 vi.mocked(printJobs.list).mockResolvedValue([{id:120,printer_id:301,job_name:'Cabinet latch',printer_name:'H2D',status:'completed'} as any])
 fireEvent.click(screen.getByRole('button',{name:'Load recent completed prints'}))
 await screen.findByRole('option',{name:'Cabinet latch · print 120 · H2D'})
 fireEvent.change(screen.getByLabelText('Recent completed print'),{target:{value:'120'}})
 expect(screen.getByLabelText('Outside print ID')).toHaveValue(120)
 fireEvent.click(screen.getByLabelText('Qualify a new recipe from this print'))
 expect(screen.getByLabelText('Observed printer ID')).toHaveValue(301)
 expect(printJobs.list).toHaveBeenCalledWith({status:'completed',limit:100})
 expect(screen.getByRole('button',{name:'Qualify recipe and bind print'})).toBeDisabled()
})
