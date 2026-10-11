import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import ArchivesPage from './Archives'
const state=vi.hoisted(()=>({data:undefined as any,loading:false,error:false,compare:undefined as any,refetch:vi.fn()}))
vi.mock('@tanstack/react-query',()=>({
 useQuery:({queryKey}:any)=>queryKey[0]==='archives'?{data:state.data,isLoading:state.loading,isError:state.error,refetch:state.refetch}:queryKey[0]==='archive-compare'?{data:state.compare,isLoading:false}:{data:[]},
 useMutation:()=>({mutate:vi.fn(),isPending:false}),useQueryClient:()=>({invalidateQueries:vi.fn()})
}))
function mount(){return render(<MemoryRouter><ArchivesPage/></MemoryRouter>)}
beforeEach(()=>{state.data=undefined;state.loading=false;state.error=false;state.compare=undefined;state.refetch.mockReset()})
afterEach(()=>cleanup())
test('successful empty archive result displays honest empty state',()=>{
 state.data={items:[],total:0};mount();expect(screen.getByText('No archived prints yet')).toBeInTheDocument()
})
test('pending archive request does not claim empty history',()=>{
 state.loading=true;mount();expect(screen.getByText('Loading...')).toBeInTheDocument();expect(screen.queryByText('No archived prints yet')).not.toBeInTheDocument()
})
test('failed archive request reports unavailable history and offers real retry',()=>{
 state.error=true;mount();expect(screen.getByRole('alert')).toHaveTextContent('Archive history unavailable');expect(screen.queryByText('No archived prints yet')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Retry'}));expect(state.refetch).toHaveBeenCalledOnce()
})
test('failed refresh with cached empty result does not assert no archived prints',()=>{
 state.error=true;state.data={items:[],total:0};mount();expect(screen.getByRole('alert')).toHaveTextContent('Archive history unavailable');expect(screen.queryByText('No archived prints yet')).not.toBeInTheDocument()
})

function archivedPrint(grams:number|null){return {id:9,print_name:'Synthetic measurement',printer_display:'Synthetic printer',status:'completed',actual_duration_seconds:60,filament_used_grams:grams,tags:[],started_at:null,completed_at:null}}
test('recorded zero filament stays distinct from unknown in table',()=>{
 state.data={items:[archivedPrint(0)],total:1};mount();const row=screen.getByText('Synthetic measurement').closest('tr')!;expect(within(row).getByText('0.0g')).toBeInTheDocument()
})
test('unknown filament remains unspecified rather than invented zero',()=>{
 state.data={items:[archivedPrint(null)],total:1};mount();expect(screen.queryByText('0.0g')).not.toBeInTheDocument();fireEvent.click(screen.getByText('Synthetic measurement'));expect(within(screen.getByRole('dialog')).queryByText('0.0g')).not.toBeInTheDocument()
})

test('recorded zero filament stays visible in archive detail',()=>{
 state.data={items:[archivedPrint(0)],total:1};mount();fireEvent.click(screen.getByText('Synthetic measurement'));expect(within(screen.getByRole('dialog')).getByText('0.0g')).toBeInTheDocument()
})
test('comparison distinguishes recorded zero filament and cost from unknown',()=>{
 const a={...archivedPrint(0),cost_estimate:0};const b={...archivedPrint(null),id:10,print_name:'Synthetic unknown',cost_estimate:null};state.data={items:[a,b],total:2};state.compare={a,b,diff:{filament_used_grams:true,cost_estimate:true}};mount();screen.getAllByRole('checkbox').forEach(box=>fireEvent.click(box));fireEvent.click(screen.getByRole('button',{name:'Compare'}));const dialog=within(screen.getByRole('dialog'));expect(dialog.getByText('0.0g')).toBeInTheDocument();expect(dialog.getByText('$0.00')).toBeInTheDocument()
})
