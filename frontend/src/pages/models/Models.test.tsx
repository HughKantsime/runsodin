import { render,screen,cleanup,fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,beforeEach,expect,test,vi } from 'vitest'
import Models from './Models'
const state=vi.hoisted(()=>({models:[] as any[]}))
vi.mock('@tanstack/react-query',()=>({useQuery:({queryKey}:any)=>queryKey[0]==='models'?{data:state.models,isLoading:false}:{data:[]},useMutation:()=>({mutate:vi.fn(),mutateAsync:vi.fn()}),useQueryClient:()=>({invalidateQueries:vi.fn()})}))
vi.mock('../../contexts/OrgContext',()=>({useOrg:()=>({orgId:1})}))
function mount(url='/models'){return render(<MemoryRouter initialEntries={[url]}><Models/></MemoryRouter>)}
beforeEach(()=>{state.models=[{id:9,name:'Synthetic gear',default_filament_type:'PETG',category:'Mechanical',is_favorite:false},{id:10,name:'Synthetic ornament',default_filament_type:'PLA',category:'Decor',is_favorite:true}]})
afterEach(()=>cleanup())
test('existing name search is case insensitive and excludes unrelated models',()=>{
 mount();fireEvent.change(screen.getByPlaceholderText('Search models...'),{target:{value:'GEAR'}});expect(screen.getByRole('heading',{name:'Synthetic gear'})).toBeInTheDocument();expect(screen.queryByRole('heading',{name:'Synthetic ornament'})).not.toBeInTheDocument()
})
test('advertised material search finds actual priced-model material',()=>{
 mount();fireEvent.change(screen.getByPlaceholderText('Search models...'),{target:{value:'petg'}});expect(screen.getByRole('heading',{name:'Synthetic gear'})).toBeInTheDocument();expect(screen.queryByRole('heading',{name:'Synthetic ornament'})).not.toBeInTheDocument()
})
test('material search loaded from URL preserves matching library result',()=>{
 mount('/models?q=PETG');expect(screen.getByRole('heading',{name:'Synthetic gear'})).toBeInTheDocument();expect(screen.getByPlaceholderText('Search models...')).toHaveValue('PETG')
})
test('favorites filter displays only persisted favorite models',()=>{
 mount('/models?favorites=true');expect(screen.getByRole('heading',{name:'Synthetic ornament'})).toBeInTheDocument();expect(screen.queryByRole('heading',{name:'Synthetic gear'})).not.toBeInTheDocument()
})
