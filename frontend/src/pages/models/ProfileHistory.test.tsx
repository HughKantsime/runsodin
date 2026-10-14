import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import ProfileHistory from './ProfileHistory'
const state=vi.hoisted(()=>({content:'current\n',historyUnavailable:false,contentUnavailable:false,diffUnavailable:false,save:vi.fn(),restore:vi.fn(),diff:vi.fn(),close:vi.fn()}))
vi.mock('../../api',()=>({profilesApi:{
 get:vi.fn(async()=>{if(state.contentUnavailable)throw new Error('Synthetic content refresh failed');return {id:101,raw_content:state.content}}),
 revisions:vi.fn(async()=>{if(state.historyUnavailable)throw new Error('Synthetic unavailable');return [{id:2,revision_number:2,created_at:'2026-10-11',created_by:3,changelog:'Current note'},{id:1,revision_number:1,created_at:'2026-10-10',created_by:null,changelog:'Original note'}]}),
 createRevision:(...args:any[])=>state.save(...args),revertRevision:(...args:any[])=>state.restore(...args),diffRevisions:(...args:any[])=>state.diff(...args),
}}))
function mount(canEdit=true){return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}><ProfileHistory profile={{id:101,name:'Synthetic profile'}} canEdit={canEdit} onClose={state.close}/></QueryClientProvider>)}
beforeEach(()=>{vi.clearAllMocks();state.content='current\n';state.historyUnavailable=false;state.contentUnavailable=false;state.diffUnavailable=false;state.save.mockImplementation(async(_id,content)=>{state.content=content;return {revision_number:3}});state.restore.mockImplementation(async()=>{state.content='original\r\n';return {revision_number:3}});state.diff.mockImplementation(async()=>{if(state.diffUnavailable)throw new Error('Synthetic comparison failure');return {diff:'-original\n+current\n'}})})
afterEach(cleanup)
async function ready(canEdit=true){mount(canEdit);await screen.findByText('Revision 2');await waitFor(()=>expect(screen.getByLabelText('Current profile content')).toHaveValue('current\n'))}

test('loads exact content and history with safe unknown actor display',async()=>{
 await ready();expect(screen.getByText('Author unavailable',{exact:false})).toBeInTheDocument();expect(screen.getByRole('button',{name:'Save revision'})).toBeDisabled();expect(screen.getByText('Original note')).toBeInTheDocument()
})
test('saves exact edits and notes then announces the confirmed revision',async()=>{
 await ready();fireEvent.change(screen.getByLabelText('Current profile content'),{target:{value:'edited\n'}});fireEvent.change(screen.getByLabelText('Change notes'),{target:{value:'Synthetic tuning'}});fireEvent.click(screen.getByRole('button',{name:'Save revision'}));await screen.findByText('Revision 3 saved');expect(state.save).toHaveBeenCalledWith(101,'edited\n','Synthetic tuning');expect(screen.getByLabelText('Current profile content')).toHaveValue('edited\n');expect(screen.getByRole('button',{name:'Save revision'})).toBeDisabled()
})
test('failed save retains edits and notes without claiming success',async()=>{
 state.save.mockRejectedValue(new Error('Synthetic save failed'));await ready();fireEvent.change(screen.getByLabelText('Current profile content'),{target:{value:'retained'}});fireEvent.change(screen.getByLabelText('Change notes'),{target:{value:'Retained note'}});fireEvent.click(screen.getByRole('button',{name:'Save revision'}));await screen.findByText('Synthetic save failed');expect(screen.getByLabelText('Current profile content')).toHaveValue('retained');expect(screen.getByLabelText('Change notes')).toHaveValue('Retained note');expect(screen.queryByText('Revision 3 saved')).not.toBeInTheDocument();expect(screen.getByRole('button',{name:'Save revision'})).toBeEnabled()
})
test('pending save disables repeat submission editing and close',async()=>{
 let finish:any;state.save.mockImplementation(()=>new Promise(resolve=>{finish=resolve}));await ready();fireEvent.change(screen.getByLabelText('Current profile content'),{target:{value:'pending'}});fireEvent.click(screen.getByRole('button',{name:'Save revision'}));await waitFor(()=>expect(screen.getByRole('button',{name:'Saving revision...'})).toBeDisabled());expect(screen.getByLabelText('Current profile content')).toBeDisabled();fireEvent.keyDown(document,{key:'Escape'});expect(state.close).not.toHaveBeenCalled();expect(state.save).toHaveBeenCalledTimes(1);finish({revision_number:3});await screen.findByText('Revision 3 saved')
})
test('viewer can compare but cannot change save or restore content',async()=>{
 await ready(false);expect(screen.getByLabelText('Current profile content')).toHaveAttribute('readonly');expect(screen.queryByRole('button',{name:'Save revision'})).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Restore revision 1'})).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Compare revision 1'}));await waitFor(()=>expect(screen.getByRole('region',{name:'Revision comparison'}).querySelector('pre')?.textContent).toBe('-original\n+current\n'));expect(state.diff).toHaveBeenCalledWith(101,1,2);expect(state.restore).not.toHaveBeenCalled();expect(state.save).not.toHaveBeenCalled()
})
test('history failure shows retry instead of an empty-history claim',async()=>{
 state.historyUnavailable=true;mount();await screen.findByText('Unable to load profile history');expect(screen.queryByText('No saved revisions yet.',{exact:false})).not.toBeInTheDocument();state.historyUnavailable=false;fireEvent.click(screen.getByRole('button',{name:'Retry history'}));await screen.findByText('Revision 2')
})
test('comparison failure offers retry and does not claim identical content',async()=>{
 state.diffUnavailable=true;await ready();fireEvent.click(screen.getByRole('button',{name:'Compare revision 1'}));await screen.findByText('Unable to compare revisions');expect(screen.queryByText('No content differences')).not.toBeInTheDocument();state.diffUnavailable=false;fireEvent.click(screen.getByRole('button',{name:'Retry comparison'}));await waitFor(()=>expect(screen.getByRole('region',{name:'Revision comparison'}).querySelector('pre')?.textContent).toBe('-original\n+current\n'))
})
test('restore requires confirmation and reloads the confirmed content',async()=>{
 await ready();fireEvent.click(screen.getByRole('button',{name:'Restore revision 1'}));await screen.findByText('Restore profile content');expect(state.restore).not.toHaveBeenCalled();fireEvent.click(screen.getByRole('button',{name:'Restore content'}));await screen.findByText('Revision 3 saved from restored content');expect(state.restore).toHaveBeenCalledWith(101,1);await waitFor(()=>expect(screen.getByLabelText('Current profile content')).toHaveValue('original\n'))
})
test('restore cancellation sends no mutation',async()=>{
 await ready();fireEvent.click(screen.getByRole('button',{name:'Restore revision 1'}));fireEvent.click(screen.getByRole('button',{name:'Cancel'}));expect(state.restore).not.toHaveBeenCalled()
})
test('restore failure preserves existing content and reports failure',async()=>{
 state.restore.mockRejectedValue(new Error('Synthetic restore failed'));await ready();fireEvent.click(screen.getByRole('button',{name:'Restore revision 1'}));fireEvent.click(screen.getByRole('button',{name:'Restore content'}));await screen.findByText('Synthetic restore failed');expect(screen.getByLabelText('Current profile content')).toHaveValue('current\n');expect(screen.queryByText('Revision 3 saved from restored content')).not.toBeInTheDocument()
})


test('restore with failed content refresh recovers fresh content without a stale draft',async()=>{
 await ready();state.restore.mockImplementation(async()=>{state.content='restored fresh';state.contentUnavailable=true;return {revision_number:3}});fireEvent.click(screen.getByRole('button',{name:'Restore revision 1'}));fireEvent.click(screen.getByRole('button',{name:'Restore content'}));await screen.findByText('Unable to load profile history');state.contentUnavailable=false;fireEvent.click(screen.getByRole('button',{name:'Retry history'}));await waitFor(()=>expect(screen.getByLabelText('Current profile content')).toHaveValue('restored fresh'));expect(screen.getByRole('button',{name:'Save revision'})).toBeDisabled();expect(state.save).not.toHaveBeenCalled()
})
