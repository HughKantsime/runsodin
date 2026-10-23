import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Setup from './Setup'
import Settings from '../admin/Settings'
const api=vi.hoisted(()=>({refresh:vi.fn(),completeOK:true,branding:true}))
vi.mock('../../permissions',()=>({refreshPermissions:api.refresh,getCurrentUser:()=>({id:1})}))
// F22-22.1-06: the instance (farm) name is Pro branding; api.branding toggles the entitlement.
vi.mock('../../LicenseContext',()=>({useLicense:()=>({isPro:api.branding,hasFeature:(feature:string)=>api.branding&&feature==='branding'})}))
vi.mock('../../api',()=>({pricingConfig:{get:vi.fn().mockResolvedValue({ui_mode:'advanced'})}}))
vi.mock('../admin/Admin', () => ({ default: () => <div>Users boundary</div> }))
vi.mock('../admin/Permissions', () => ({ default: () => <div>Permissions boundary</div> }))
vi.mock('../admin/Branding', () => ({ default: () => <div>Branding boundary</div> }))
vi.mock('../../components/admin/OIDCSettings', () => ({ default: () => <div>OIDC boundary</div> }))
vi.mock('../../components/admin/WebhookSettings', () => ({ default: () => <div>Integrations boundary</div> }))
vi.mock('../../components/admin/GroupManager', () => ({ default: () => <div>Groups boundary</div> }))
vi.mock('../../components/auth/MFASetup', () => ({ default: () => <div>MFA boundary</div> }))
vi.mock('../../components/auth/APITokenManager', () => ({ default: () => <div>Tokens boundary</div> }))
vi.mock('../../components/auth/SessionManager', () => ({ default: () => <div>Sessions boundary</div> }))
vi.mock('../../components/admin/QuotaManager', () => ({ default: () => <div>Quotas boundary</div> }))
vi.mock('../../components/admin/IPAllowlistSettings', () => ({ default: () => <div>Allowlist boundary</div> }))
vi.mock('../../components/admin/OrgManager', () => ({ default: () => <div>Organizations boundary</div> }))
vi.mock('../../components/admin/LogViewer', () => ({ default: () => <div>Logs boundary</div> }))
vi.mock('../../components/admin/NotificationsTab', () => ({ default: () => <div>Notifications boundary</div> }))
vi.mock('../../components/admin/VisionSettingsTab', () => ({ default: () => <div>Vigil boundary</div> }))
vi.mock('../../components/admin/SystemTab', () => ({ default: () => <div>System boundary</div> }))
vi.mock('../../components/admin/LicenseTab', () => ({ default: () => <div>License boundary</div> }))
vi.mock('../../components/admin/GeneralTab',()=>({default:()=>null}))
beforeEach(()=>{
 api.completeOK=true;api.branding=true;api.refresh.mockResolvedValue({page_access:{}})
 vi.stubGlobal('fetch',vi.fn(async(url:unknown)=>({ok:String(url)==='/api/setup/complete'?api.completeOK:true,status:api.completeOK?200:500,json:async()=>String(url)==='/api/setup/network'?{detected_ip:'192.0.2.10'}:{access_token:'synthetic-test-authority'}})))
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals()})
function mount(){render(<MemoryRouter initialEntries={['/setup']}><Routes><Route path='/setup' element={<Setup/>}/><Route path='/' element={<h1>Synthetic Dashboard</h1>}/></Routes></MemoryRouter>)}
async function admin(){
 fireEvent.click(screen.getByRole('button',{name:'Get Started'}))
 fireEvent.change(screen.getByPlaceholderText('admin'),{target:{value:' synthetic-admin '}})
 fireEvent.change(screen.getByPlaceholderText('you@example.com'),{target:{value:' synthetic@example.invalid '}})
 fireEvent.change(screen.getByPlaceholderText('Min 8 characters (uppercase, lowercase, number)'),{target:{value:'Synthetic123'}})
 fireEvent.change(screen.getByPlaceholderText('Repeat password'),{target:{value:'Synthetic123'}})
 if(api.branding)fireEvent.change(screen.getByPlaceholderText('O.D.I.N.'),{target:{value:'Synthetic farm'}})
 fireEvent.click(screen.getByRole('button',{name:'Create Account'}))
 await screen.findByRole('heading',{name:'Pick a Tier'})
}
async function ready(){await admin();fireEvent.click(screen.getByText('Community (free)'));fireEvent.click(screen.getByRole('button',{name:'Start with Community'}));expect(screen.getByRole('heading',{name:'Add a Printer'})).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:/Skip.*add printers later/}));await screen.findByRole('heading',{name:'Network Configuration'});fireEvent.click(screen.getByRole('button',{name:'Skip',exact:true}));expect(screen.getByRole('heading',{name:"You're all set!"})).toBeInTheDocument()}
test('F22-22.1-06 required admin fields block requests; Back returns welcome',()=>{
 mount();fireEvent.click(screen.getByRole('button',{name:'Get Started'}));fireEvent.click(screen.getByRole('button',{name:'Create Account'}))
 expect(screen.getByText('Username is required')).toBeInTheDocument();expect(fetch).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Back'}));expect(screen.getByRole('heading',{name:'Welcome to O.D.I.N.'})).toBeInTheDocument()
})
test('F22-22.1-06 successful account request preserves farm name and establishes cookie login',async()=>{
 mount();await admin()
 const calls=vi.mocked(fetch).mock.calls
 expect(JSON.parse(String(calls[0][1]?.body))).toEqual({username:'synthetic-admin',email:'synthetic@example.invalid',password:'Synthetic123',site_name:'Synthetic farm'})
 expect(calls[1][0]).toBe('/api/auth/login');expect(calls[1][1]?.credentials).toBe('include');expect(api.refresh).toHaveBeenCalledOnce()
})
test('F22-22.1-06 Community flow skips physical printer and network, sends completion and navigates',async()=>{
 mount();await ready();fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}));await screen.findByRole('heading',{name:'Synthetic Dashboard'})
 expect(vi.mocked(fetch).mock.calls.some(([url])=>url==='/api/setup/printer'||url==='/api/setup/test-printer')).toBe(false)
 expect(fetch).toHaveBeenCalledWith('/api/setup/complete',expect.objectContaining({method:'POST',credentials:'include'}))
})
test('F22-22.1-06 below Pro the farm-name field is disabled with a Pro note and no name is sent',async()=>{
 api.branding=false;mount();fireEvent.click(screen.getByRole('button',{name:'Get Started'}))
 const field=screen.getByRole('textbox',{name:'Instance Name'})
 expect(field).toBeDisabled();expect(field).toHaveValue('O.D.I.N.')
 expect(screen.getByText(/Pro feature: a custom instance name needs a Pro license/)).toBeInTheDocument()
 cleanup();mount();await admin()
 expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))).toEqual({username:'synthetic-admin',email:'synthetic@example.invalid',password:'Synthetic123'})
})
test('F22-22.1-06 farm name is saved with Admin before the documented Tier step',async()=>{
 mount();await admin()
 expect(screen.getByRole('heading',{name:'Pick a Tier'})).toBeInTheDocument()
 expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).site_name).toBe('Synthetic farm')
})
test('F22-22.1-06 rejected completion does not enter dashboard as completed',async()=>{
 mount();await ready();api.completeOK=false;fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}))
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/setup/complete',expect.anything()))
 await screen.findByText('Could not confirm setup completion. Please try again.')
 expect(screen.queryByRole('heading',{name:'Synthetic Dashboard'})).not.toBeInTheDocument()
})
test('F22-22.6-04 real Settings header exposes contextual documentation link',()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}})
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/settings']}><Settings/></MemoryRouter></QueryClientProvider>)
 const link=screen.getByTitle('Settings')
 expect(link).toHaveAttribute('href','https://docs.runsodin.com/configuration/settings');expect(link).toHaveAttribute('target','_blank');expect(link).toHaveAttribute('rel','noopener noreferrer')
})

test('completion saved but refresh null retries permissions without repeating completion POST',async()=>{
 mount();await ready();api.refresh.mockResolvedValueOnce(null).mockResolvedValueOnce({page_access:{}})
 fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}))
 await screen.findByText('Setup is saved, but permissions could not be loaded. Please try again.')
 expect(screen.queryByRole('heading',{name:'Synthetic Dashboard'})).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}))
 await screen.findByRole('heading',{name:'Synthetic Dashboard'})
 expect(vi.mocked(fetch).mock.calls.filter(([url])=>url==='/api/setup/complete')).toHaveLength(1)
})
test.each([false,true])('lost completion response requires explicit persisted status %s',async complete=>{
 mount();await ready();const original=vi.mocked(fetch).getMockImplementation()!
 vi.mocked(fetch).mockImplementation(async(url,opts)=>{
  if(url==='/api/setup/complete')throw new Error('Synthetic lost response')
  if(url==='/api/setup/status')return {ok:true,json:async()=>({is_complete:complete})} as Response
  return original(url,opts)
 })
 fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}))
 if(complete)await screen.findByRole('heading',{name:'Synthetic Dashboard'})
 else {await screen.findByText('Could not confirm setup completion. Please try again.');expect(screen.queryByRole('heading',{name:'Synthetic Dashboard'})).not.toBeInTheDocument()}
})
test('pending completion prevents duplicate clicks until confirmed',async()=>{
 mount();await ready();let resolve!:(value:Response)=>void
 const original=vi.mocked(fetch).getMockImplementation()!
 vi.mocked(fetch).mockImplementation((url,opts)=>url==='/api/setup/complete'?new Promise(done=>{resolve=done}):original(url,opts))
 fireEvent.click(screen.getByRole('button',{name:'Open Dashboard'}))
 const pending=screen.getByRole('button',{name:'Opening Dashboard...'})
 expect(pending).toBeDisabled();fireEvent.click(pending)
 expect(vi.mocked(fetch).mock.calls.filter(([url])=>url==='/api/setup/complete')).toHaveLength(1)
 resolve({ok:true} as Response);await screen.findByRole('heading',{name:'Synthetic Dashboard'})
})

test('wizard sends the backend PrusaLink schema for both connection test and creation', async () => {
 const { default: payload } = await import('../../../../tests/fixtures/setup_prusalink_payload.json')
 mount(); await admin()
 fireEvent.click(screen.getByText('Community (free)'))
 fireEvent.click(screen.getByRole('button',{name:'Start with Community'}))
 fireEvent.click(screen.getByRole('button',{name:'Prusa',exact:true}))
 fireEvent.change(screen.getByRole('combobox'),{target:{value:'MK4'}})
 fireEvent.change(screen.getByPlaceholderText('e.g. X1C - Left Shelf'),{target:{value:payload.name}})
 fireEvent.change(screen.getByPlaceholderText('192.168.1.100'),{target:{value:payload.api_host}})
 const key = screen.getByLabelText('PrusaLink API Key')
 expect(key).toHaveAttribute('type','password')
 fireEvent.change(key,{target:{value:payload.api_key}})
 fireEvent.click(screen.getByRole('button',{name:'Test Connection'}))
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/setup/test-printer',expect.objectContaining({body:JSON.stringify(payload)})))
 fireEvent.click(screen.getByRole('button',{name:'Add Printer',exact:true}))
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/setup/printer',expect.objectContaining({body:JSON.stringify(payload)})))
})

test('wizard packs Bambu credentials for creation and keeps separate probe credentials', async () => {
 mount(); await admin()
 fireEvent.click(screen.getByText('Community (free)'))
 fireEvent.click(screen.getByRole('button',{name:'Start with Community'}))
 fireEvent.change(screen.getByPlaceholderText('e.g. X1C - Left Shelf'),{target:{value:'Fixture Bambu'}})
 fireEvent.change(screen.getByPlaceholderText('192.168.1.100'),{target:{value:'192.0.2.45'}})
 fireEvent.change(screen.getByPlaceholderText('From printer settings or Bambu app'),{target:{value:'FIXTURE-SERIAL'}})
 fireEvent.change(screen.getByPlaceholderText('8-digit code from printer'),{target:{value:'fixture-code'}})
 fireEvent.click(screen.getByRole('button',{name:'Test Connection'}))
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/setup/test-printer',expect.objectContaining({body:expect.any(String)})))
 fireEvent.click(screen.getByRole('button',{name:'Add Printer',exact:true}))
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/setup/printer',expect.objectContaining({body:expect.any(String)})))
 for(const endpoint of ['/api/setup/test-printer','/api/setup/printer']) {
  const call=vi.mocked(fetch).mock.calls.find(([url])=>url===endpoint)!
  expect(JSON.parse(String(call[1]?.body))).toMatchObject({api_type:'bambu',api_host:'192.0.2.45',api_key:'FIXTURE-SERIAL|fixture-code',serial:'FIXTURE-SERIAL',access_code:'fixture-code'})
 }
})
