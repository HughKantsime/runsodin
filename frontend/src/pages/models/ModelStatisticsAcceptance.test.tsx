import {render,screen,cleanup,fireEvent,within} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import Models from './Models'
const state=vi.hoisted(()=>({models:[] as any[]}))
vi.mock('@tanstack/react-query',()=>({useQuery:({queryKey}:any)=>queryKey[0]==='models'?{data:state.models,isLoading:false}:{data:[]},useMutation:()=>({mutate:vi.fn(),mutateAsync:vi.fn()}),useQueryClient:()=>({invalidateQueries:vi.fn()})}))
vi.mock('../../contexts/OrgContext',()=>({useOrg:()=>({orgId:1})}))
function mount(url='/models'){render(<MemoryRouter initialEntries={[url]}><Models/></MemoryRouter>);return screen.getByLabelText('Selected model job statistics')}
function model(id:number,name:string,count:number,hours:number,unknown:number,favorite=false){return {id,name,category:'Synthetic',is_favorite:favorite,job_statistics:{completed_job_count:count,actual_print_hours:hours,unknown_timing_job_count:unknown}}}
beforeEach(()=>{state.models=[model(1,'Gear',2,3,0),model(2,'Ornament',4,2,1,true)]})
afterEach(cleanup)
test('rendered summary labels restricted visible generic measured data',()=>{
 const summary=mount();expect(within(summary).getByText('Completed jobs')).toBeInTheDocument();expect(within(summary).getByText('6')).toBeInTheDocument();expect(within(summary).getByText('5.0h')).toBeInTheDocument();expect(within(summary).getByText(/Education jobs excluded/)).toBeInTheDocument();expect(within(summary).getByText(/Timing unknown for 1 completed job;/)).toBeInTheDocument()
})
test('search and favorites select the same models as job totals',()=>{
 const summary=mount();fireEvent.change(screen.getByPlaceholderText('Search models...'),{target:{value:'gear'}});expect(within(summary).getByText('2')).toBeInTheDocument();expect(within(summary).getByText('3.0h')).toBeInTheDocument();expect(within(summary).queryByText(/Timing unknown/)).not.toBeInTheDocument()
 cleanup();const favorites=mount('/models?favorites=true');expect(within(favorites).getByText('4')).toBeInTheDocument();expect(within(favorites).getByText('2.0h')).toBeInTheDocument()
})
test('known no completed jobs displays zero',()=>{state.models=[model(1,'Gear',0,0,0)];const summary=mount();expect(within(summary).getByText('0')).toBeInTheDocument();expect(within(summary).getByText('0.0h')).toBeInTheDocument()})
test('missing jobs scope is unavailable and never displayed as zero',()=>{state.models=[{id:1,name:'Gear',job_statistics:null}];const summary=mount();expect(within(summary).getAllByText('Unavailable')).toHaveLength(2);expect(within(summary).queryByText('0.0h')).not.toBeInTheDocument()})
test('completed jobs with no valid measured timing display unknown',()=>{state.models=[model(1,'Gear',2,0,2)];const summary=mount();expect(within(summary).getByText('Unknown')).toBeInTheDocument();expect(within(summary).getByText(/Timing unknown for 2 completed jobs/)).toBeInTheDocument();expect(within(summary).queryByText('0.0h')).not.toBeInTheDocument()})
