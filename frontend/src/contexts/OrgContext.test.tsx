import { beforeEach, afterEach, test, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OrgProvider, useOrg } from './OrgContext'
let client:QueryClient
function Consumer(){const org=useOrg();return <><output data-testid="context">{JSON.stringify({orgId:org.orgId,orgName:org.orgName,isAdmin:org.isAdmin,loading:org.loading})}</output><button onClick={()=>org.switchOrg(1)}>Select one</button><button onClick={()=>org.switchOrg(999)}>Select invalid</button></>}
function Harness(){return <QueryClientProvider client={client}><OrgProvider><Consumer/></OrgProvider></QueryClientProvider>}
const context=()=>JSON.parse(screen.getByTestId('context').textContent!)
beforeEach(()=>{client=new QueryClient({defaultOptions:{queries:{retry:false}}});client.setQueryData(['auth','me'],{id:3,role:'admin',group_id:null});vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>[{id:1,name:'Synthetic one'},{id:2,name:'Synthetic two'}]})))})
afterEach(()=>{client.clear();vi.unstubAllGlobals()})
test('known superadmin selection rejects unavailable organizations',async()=>{render(<Harness/>);await waitFor(()=>expect(context().loading).toBe(false));fireEvent.click(screen.getByRole('button',{name:'Select one'}));expect(context().orgId).toBe(1);fireEvent.click(screen.getByRole('button',{name:'Select invalid'}));expect(context().orgId).toBe(1)})
test('new account resets old selected org and tenant cannot select foreign org',async()=>{
 render(<Harness/>);await waitFor(()=>expect(context().loading).toBe(false));fireEvent.click(screen.getByRole('button',{name:'Select one'}))
 await act(async()=>{client.setQueryData(['auth','me'],{id:4,role:'viewer',group_id:2})});await waitFor(()=>expect(context()).toMatchObject({orgId:2,isAdmin:false}));fireEvent.click(screen.getByRole('button',{name:'Select one'}));expect(context().orgId).toBe(2)
})
test('late previous-account org response cannot replace new context',async()=>{
 let resolve:any;let signal:AbortSignal
 vi.stubGlobal('fetch',vi.fn((_url,options)=>{signal=options.signal;return new Promise(done=>{resolve=done})}))
 render(<Harness/>);await waitFor(()=>expect(fetch).toHaveBeenCalled())
 await act(async()=>{client.setQueryData(['auth','me'],{id:4,role:'viewer',group_id:2})});await waitFor(()=>expect(signal!.aborted).toBe(true))
 await act(async()=>{resolve({ok:true,json:async()=>[{id:1,name:'Synthetic old organization'}]})});expect(context()).toMatchObject({orgId:2,isAdmin:false,orgName:null})
})
test('unknown principal does not request or expose orgs',()=>{client.setQueryData(['auth','me'],null);render(<Harness/>);expect(context()).toMatchObject({orgId:null,isAdmin:false});expect(fetch).not.toHaveBeenCalled()})
