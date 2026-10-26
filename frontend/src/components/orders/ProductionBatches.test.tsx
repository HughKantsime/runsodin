import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,it,expect,vi} from 'vitest'
import ProductionBatches from './ProductionBatches'
import {production} from '../../api/production'
import {square} from '../../api/square'
const identity=vi.hoisted(()=>({role:'admin'}))
vi.mock('../../permissions',()=>({getCurrentUser:()=>identity}))
vi.mock('../../api/production',()=>({production:{state:vi.fn(),jobs:vi.fn(),recipe:vi.fn(),inspect:vi.fn()}}))
vi.mock('../../api/square',()=>({square:{status:vi.fn()}}))
const recipe={id:'a'.repeat(32),name:'Mini batch',materials:'PLA black',qualified:true,reference_job_id:1,outputs:[{product_id:101,product_name:'Mini',kind:'main' as const,expected:5}]}
const state={recipes:[recipe],receipts:[],additions:[],square_writes_enabled:false,next_offset:null}
const jobs={jobs:[{id:2,name:'Finished mini',payload:{printer_id:1,plate_index:0,payload_sha256:'b'.repeat(64)}}],next_offset:null}
async function mount(){render(<ProductionBatches products={[{id:101,name:'Mini'}]}/>);fireEvent.click(screen.getByRole('button',{name:'Recipes and finished batches'}));await screen.findByRole('option',{name:'Mini batch · aaaaaaaa'})}
async function counted(){await mount();fireEvent.change(screen.getByLabelText('Qualified recipe'),{target:{value:recipe.id}});fireEvent.change(screen.getByLabelText('Completed batch job'),{target:{value:'2'}});fireEvent.change(screen.getByLabelText('Mini main accepted'),{target:{value:'4'}});fireEvent.change(screen.getByLabelText('Mini main rejected'),{target:{value:'1'}})}
beforeEach(()=>{vi.resetAllMocks();identity.role='admin';vi.mocked(production.state).mockResolvedValue(state);vi.mocked(production.jobs).mockResolvedValue(jobs);vi.mocked(square.status).mockResolvedValue({connected:true,revision:'r1',mappings:[]})})
afterEach(cleanup)
it('closed panel makes no requests',()=>{render(<ProductionBatches products={[]}/>);expect(production.state).not.toHaveBeenCalled();expect(square.status).not.toHaveBeenCalled()})
it('inspection requires explicit counts and physical confirmation',async()=>{
 await counted();const submit=screen.getByRole('button',{name:'Record inspection'});expect(submit).toBeDisabled();fireEvent.click(screen.getByLabelText(/I inspected this physical batch/));expect(submit).toBeEnabled()
 vi.mocked(production.inspect).mockResolvedValue({id:'receipt1',recipe_id:recipe.id,job_id:2,recipe_name:'Mini batch',occurred_at:'2026-10-20T12:00:00Z',stock_status:'prepared',outputs:[]})
 fireEvent.click(submit);await waitFor(()=>expect(production.inspect).toHaveBeenCalledWith({recipe_id:recipe.id,job_id:2,square_revision:'r1',inspection_confirmed:true,outputs:[{product_id:101,kind:'main',accepted:4,rejected:1}]}));await screen.findByText(/Square addition prepared, not sent/);expect(screen.getByLabelText(/I inspected this physical batch/)).not.toBeChecked()
})
it('failed acceptance retains entered counts without success claim',async()=>{
 await counted();fireEvent.click(screen.getByLabelText(/I inspected this physical batch/));vi.mocked(production.inspect).mockRejectedValue(new Error('Physical batch already recorded'));fireEvent.click(screen.getByRole('button',{name:'Record inspection'}));await screen.findByRole('alert');expect(screen.getByLabelText('Mini main accepted')).toHaveValue(4);expect(screen.queryByText(/Inspection recorded for job/)).not.toBeInTheDocument()
})
it('viewer reads without qualification or inspection controls',async()=>{
 identity.role='viewer';render(<ProductionBatches products={[]}/>);fireEvent.click(screen.getByRole('button',{name:'Recipes and finished batches'}));await screen.findByText(/Mini batch · Qualified/);expect(production.jobs).not.toHaveBeenCalled();expect(screen.queryByRole('button',{name:'Record inspection'})).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Save recipe revision'})).not.toBeInTheDocument()
})
it('draft recipe does not invent yield qualification',async()=>{
 await mount();fireEvent.click(screen.getByRole('button',{name:'Create recipe revision'}));fireEvent.change(screen.getByLabelText('Recipe name'),{target:{value:'New mini'}});fireEvent.change(screen.getByLabelText('Successful reference job'),{target:{value:'2'}});fireEvent.change(screen.getByLabelText('Material and color requirements'),{target:{value:'PETG red'}});fireEvent.change(screen.getByLabelText('Output 1 product'),{target:{value:'101'}});fireEvent.change(screen.getByLabelText('Output 1 expected pieces'),{target:{value:'5'}});vi.mocked(production.recipe).mockResolvedValue({...recipe,qualified:false});fireEvent.click(screen.getByRole('button',{name:'Save recipe revision'}));await waitFor(()=>expect(production.recipe).toHaveBeenCalledWith({reference_job_id:2,name:'New mini',materials:'PETG red',qualification_confirmed:false,outputs:[{product_id:101,kind:'main',expected:5}]}));await screen.findByText(/Draft recipe saved/)
})

it('rejected-only batch does not depend on Square being available',async()=>{
 await counted();fireEvent.change(screen.getByLabelText('Mini main accepted'),{target:{value:'0'}});fireEvent.change(screen.getByLabelText('Mini main rejected'),{target:{value:'5'}});fireEvent.click(screen.getByLabelText(/I inspected this physical batch/));vi.mocked(square.status).mockRejectedValue(new Error('Unavailable'))
 vi.mocked(production.inspect).mockResolvedValue({id:'rejected',recipe_id:recipe.id,job_id:2,recipe_name:'Mini batch',occurred_at:'2026-10-20T12:00:00Z',stock_status:'no_sellable_output',outputs:[]})
 fireEvent.click(screen.getByRole('button',{name:'Record inspection'}));await screen.findByText(/No sellable pieces added/);expect(square.status).not.toHaveBeenCalled();expect(production.inspect).toHaveBeenCalledWith(expect.objectContaining({square_revision:null}))
})
it('inspection deep link opens the requested job and matches the exact qualified payload',async()=>{
 vi.mocked(production.state).mockResolvedValue({...state,recipes:[{...recipe,payload:jobs.jobs[0].payload}]})
 render(<ProductionBatches products={[]} inspectJobId={2}/>)
 await waitFor(()=>expect(screen.getByLabelText('Completed batch job')).toHaveValue('2'))
 expect(production.jobs).toHaveBeenCalledWith(0,2)
 expect(screen.getByLabelText('Qualified recipe')).toHaveValue(recipe.id)
 expect(screen.getByRole('button',{name:'Record inspection'})).toBeDisabled()
})
it('inspection deep link reports an unavailable job without substituting another',async()=>{
 vi.mocked(production.jobs).mockRejectedValue(new Error('This job has multiple physical attempts.'))
 render(<ProductionBatches products={[]} inspectJobId={900}/>)
 await screen.findByRole('alert')
 expect(screen.getByLabelText('Completed batch job')).toHaveValue('')
})
it('lists and inspects an owner-attested outside print by physical ID',async()=>{
 vi.mocked(production.jobs).mockResolvedValue({jobs:[{kind:'outside_print',print_job_id:120,name:'Cabinet lock v3.1',recipe,payload:{printer_id:1}}],next_offset:null})
 await mount()
 fireEvent.change(screen.getByLabelText('Qualified recipe'),{target:{value:recipe.id}})
 fireEvent.change(screen.getByLabelText('Completed batch job'),{target:{value:'outside:120'}})
 fireEvent.change(screen.getByLabelText('Mini main accepted'),{target:{value:'4'}})
 fireEvent.change(screen.getByLabelText('Mini main rejected'),{target:{value:'1'}})
 fireEvent.click(screen.getByLabelText(/I inspected this physical batch/))
 vi.mocked(production.inspect).mockResolvedValue({id:'outside',recipe_id:recipe.id,print_job_id:120,recipe_name:recipe.name,occurred_at:'2026-10-26T17:07:38Z',stock_status:'prepared',outputs:[]})
 fireEvent.click(screen.getByRole('button',{name:'Record inspection'}))
 await waitFor(()=>expect(production.inspect).toHaveBeenCalledWith({recipe_id:recipe.id,print_job_id:120,square_revision:'r1',inspection_confirmed:true,outputs:[{product_id:101,kind:'main',accepted:4,rejected:1}]}))
 await screen.findByText(/Inspection recorded for outside print 120/)
})
