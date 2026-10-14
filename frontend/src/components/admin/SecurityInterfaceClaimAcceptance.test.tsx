import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import GroupManager from './GroupManager'
import QuotaManager from './QuotaManager'
const api=vi.hoisted(()=>({groupList:vi.fn(),create:vi.fn(),update:vi.fn(),remove:vi.fn(),userList:vi.fn(),quotaList:vi.fn(),quotaSet:vi.fn(),hasGroups:true}))
vi.mock('../../api',()=>({groups:{list:api.groupList,create:api.create,update:api.update,delete:api.remove},users:{list:api.userList},quotas:{adminList:api.quotaList,adminSet:api.quotaSet}}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({hasFeature:()=>api.hasGroups})}))
beforeEach(()=>{
 api.hasGroups=true
 api.groupList.mockResolvedValue([{id:7,name:'Synthetic class',description:'Synthetic group',owner_id:2,owner_username:'Synthetic operator',member_count:3}])
 api.userList.mockResolvedValue([{id:2,username:'Synthetic operator',role:'operator'},{id:3,username:'Synthetic admin',role:'admin'},{id:4,username:'Synthetic viewer',role:'viewer'}])
 api.create.mockResolvedValue({});api.update.mockResolvedValue({});api.remove.mockResolvedValue({})
 api.quotaList.mockResolvedValue([{user_id:9,username:'Synthetic student',quota_jobs:10,quota_grams:100,quota_hours:5,quota_period:'monthly',usage:{jobs_used:2,grams_used:20,hours_used:1}}])
 api.quotaSet.mockResolvedValue({})
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals()})
function mountGroup(){const c=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});return render(<QueryClientProvider client={c}><GroupManager/></QueryClientProvider>)}
async function quotaRow(){render(<QuotaManager/>);return (await screen.findByText('Synthetic student')).closest('tr')!}
test('F11-11.4-01 group table renders owner and membership count; edit saves fixed group',async()=>{
 mountGroup();await screen.findByText('Synthetic class')
 expect(screen.getByText('Synthetic operator')).toBeInTheDocument()
 fireEvent.click(screen.getByTitle('Edit'))
 const dialog=screen.getByRole('dialog',{name:'Edit Group'})
 fireEvent.change(within(dialog).getByPlaceholderText("e.g. Mrs. Smith's Class"),{target:{value:'Synthetic renamed'}})
 fireEvent.click(within(dialog).getByRole('button',{name:'Save Changes'}))
 await waitFor(()=>expect(api.update).toHaveBeenCalledExactlyOnceWith(7,{name:'Synthetic renamed',description:'Synthetic group',owner_id:2}))
 await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
test('F11-11.4-02 owner options exclude viewer and creation serializes selected approver id',async()=>{
 mountGroup();await screen.findByText('Synthetic class')
 fireEvent.click(screen.getByRole('button',{name:'New Group'}))
 const dialog=screen.getByRole('dialog',{name:'New Group'})
 expect(within(dialog).getAllByRole('option').map(x=>x.textContent)).toEqual(['No owner','Synthetic operator (operator)','Synthetic admin (admin)'])
 fireEvent.change(within(dialog).getByPlaceholderText("e.g. Mrs. Smith's Class"),{target:{value:'Synthetic new'}})
 fireEvent.change(within(dialog).getByRole('combobox'),{target:{value:'3'}})
 fireEvent.click(within(dialog).getByRole('button',{name:'Create Group'}))
 await waitFor(()=>expect(api.create).toHaveBeenCalledExactlyOnceWith({name:'Synthetic new',description:'',owner_id:3}))
})
test('F11-11.4-01 delete cancel prevents mutation; confirmation deletes selected group',async()=>{
 const confirm=vi.fn().mockReturnValue(false);vi.stubGlobal('confirm',confirm)
 mountGroup();await screen.findByText('Synthetic class')
 fireEvent.click(screen.getByTitle('Delete'));expect(api.remove).not.toHaveBeenCalled()
 confirm.mockReturnValue(true);fireEvent.click(screen.getByTitle('Delete'))
 await waitFor(()=>expect(api.remove).toHaveBeenCalledExactlyOnceWith(7))
})
test('F11-11.4-01 feature gate suppresses UI and group/user queries',()=>{
 api.hasGroups=false;const view=mountGroup()
 expect(view.container).toBeEmptyDOMElement()
 expect(api.groupList).not.toHaveBeenCalled();expect(api.userList).not.toHaveBeenCalled()
})
test('F11-11.8-01/04 configuration renders usage and serializes jobs/grams/hours edits',async()=>{
 const row=await quotaRow()
 expect(within(row).getByText('2/10 jobs (20%)')).toBeInTheDocument()
 expect(within(row).getByText('20/100g')).toBeInTheDocument()
 expect(within(row).getByText('1.0/5h')).toBeInTheDocument()
 const fields=within(row).getAllByRole('spinbutton')
 for(const [i,value] of ['15','250','7.5'].entries())fireEvent.change(fields[i],{target:{value}})
 fireEvent.click(within(row).getByRole('button'))
 await waitFor(()=>expect(api.quotaSet).toHaveBeenCalledExactlyOnceWith(9,{quota_jobs:15,quota_grams:250,quota_hours:7.5}))
 await waitFor(()=>expect(within(row).queryByRole('button')).not.toBeInTheDocument())
})
test.each(['daily','weekly','monthly','semester'])('F11-11.8-02 configuration saves %s period',async period=>{
 const row=await quotaRow()
 fireEvent.change(within(row).getByRole('combobox',{name:'Quota period for Synthetic student'}),{target:{value:period}})
 fireEvent.click(within(row).getByRole('button'))
 await waitFor(()=>expect(api.quotaSet).toHaveBeenCalledExactlyOnceWith(9,{quota_period:period}))
})
test('F11-11.8-04 clearing numeric quota sends null',async()=>{
 const row=await quotaRow()
 fireEvent.change(within(row).getAllByRole('spinbutton')[0],{target:{value:''}})
 fireEvent.click(within(row).getByRole('button'))
 await waitFor(()=>expect(api.quotaSet).toHaveBeenCalledExactlyOnceWith(9,{quota_jobs:null}))
})
// Preserve missing advertised form associations as a failing acceptance check.
test('F12-12.7-05 group Name label is associated with its input',async()=>{
 mountGroup();await screen.findByText('Synthetic class')
 fireEvent.click(screen.getByRole('button',{name:'New Group'}))
 expect(within(screen.getByRole('dialog')).queryByRole('textbox',{name:'Name',exact:true})).toBeInTheDocument()
})
