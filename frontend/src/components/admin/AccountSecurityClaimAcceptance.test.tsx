import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Login from '../../pages/auth/Login'
import SystemTab from './SystemTab'
const api=vi.hoisted(()=>({capabilities:vi.fn(),exportData:vi.fn(),eraseData:vi.fn(),fetchAPI:vi.fn()}))
vi.mock('../../api',()=>({auth:{capabilities:api.capabilities},gdpr:{exportData:api.exportData,eraseData:api.eraseData},fetchAPI:api.fetchAPI,config:{getNetwork:vi.fn().mockResolvedValue({})},backups:{list:vi.fn().mockResolvedValue([])},downloadBlob:vi.fn()}))
vi.mock('../../permissions',()=>({refreshPermissions:vi.fn(),clearSensitiveBrowserState:vi.fn()}))
vi.mock('../../BrandingContext',()=>({useBranding:()=>({app_name:'Synthetic farm',app_subtitle:'Synthetic'})}))
vi.mock('../../components/auth/SSOButton',()=>({default:()=>null}))
vi.mock('./BackupRestore',()=>({default:()=>null}))
vi.mock('./TroubleshootingPanel',()=>({default:()=>null}))
vi.mock('./DataRetentionSettings',()=>({default:()=>null}))
vi.mock('./ReportScheduleManager',()=>({default:()=>null}))
vi.mock('./ChargebackReport',()=>({default:()=>null}))
beforeEach(()=>{
 api.capabilities.mockResolvedValue({smtp_enabled:false})
 api.fetchAPI.mockImplementation(async path=>path==='/auth/me'?{id:21}: {})
 api.exportData.mockResolvedValue({user:{id:21,username:'Synthetic person'},jobs:[{id:99}],sessions:[],preferences:{theme:'dark'}})
 vi.stubGlobal('__APP_VERSION__','synthetic')
 vi.stubGlobal('matchMedia',vi.fn().mockReturnValue({matches:false}))
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals();vi.restoreAllMocks()})
function login(){render(<MemoryRouter><Login/></MemoryRouter>)}
function system(){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});render(<QueryClientProvider client={client}><SystemTab/></QueryClientProvider>)}
test('F11-11.1-08 smtp capability enables forgot-password form',async()=>{
 api.capabilities.mockResolvedValue({smtp_enabled:true});login()
 const button=await screen.findByRole('button',{name:'Forgot your password?'})
 expect(api.capabilities).toHaveBeenCalledOnce()
 fireEvent.click(button)
 expect(screen.getByRole('textbox',{name:'Account email'})).toBeInTheDocument()
})
test('F11-11.1-08 disabled smtp gives administrator guidance without reset action',async()=>{
 login();await waitFor(()=>expect(api.capabilities).toHaveBeenCalledOnce())
 expect(screen.getByText('Forgot your password? Contact your administrator.')).toBeInTheDocument()
 expect(screen.queryByRole('button',{name:'Forgot your password?'})).not.toBeInTheDocument()
})
test('F11-11.1-08 failed capabilities fails closed to administrator guidance',async()=>{
 api.capabilities.mockRejectedValue(new Error('Synthetic unavailable'));login()
 await waitFor(()=>expect(api.capabilities).toHaveBeenCalledOnce())
 expect(screen.queryByRole('button',{name:'Forgot your password?'})).not.toBeInTheDocument()
 expect(screen.getByText('Forgot your password? Contact your administrator.')).toBeInTheDocument()
})
test('F12-12.4-01 exports current authenticated user JSON via temporary download URL',async()=>{
 let blob:Blob|undefined
 const create=vi.fn((value:Blob)=>{blob=value;return 'blob:synthetic-export'})
 const revoke=vi.fn();vi.stubGlobal('URL',class extends URL { static createObjectURL=create;static revokeObjectURL=revoke })
 const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(function(){expect(this.download).toMatch(/^odin-my-data-\d{4}-\d{2}-\d{2}\.json$/);expect(this.href).toBe('blob:synthetic-export')})
 system();fireEvent.click(screen.getByRole('button',{name:'Export My Data'}))
 await screen.findByText('Data exported successfully')
 expect(api.fetchAPI).toHaveBeenCalledWith('/auth/me')
 expect(api.exportData).toHaveBeenCalledExactlyOnceWith(21)
 expect(blob?.type).toBe('application/json')
 const reader=new FileReader()
 const contents=await new Promise<string>((resolve,reject)=>{reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsText(blob!)})
 expect(JSON.parse(contents)).toEqual(await api.exportData.mock.results[0].value)
 expect(click).toHaveBeenCalledOnce();expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:synthetic-export')
})
test('F12-12.4-01 missing authenticated identity prevents export',async()=>{
 api.fetchAPI.mockResolvedValue({});system()
 fireEvent.click(screen.getByRole('button',{name:'Export My Data'}))
 await screen.findByText('Could not determine user ID')
 expect(api.exportData).not.toHaveBeenCalled()
})
test('F12-12.4-01 export failure reports error and restores retry control',async()=>{
 api.exportData.mockRejectedValue(new Error('Synthetic export failure'));system()
 fireEvent.click(screen.getByRole('button',{name:'Export My Data'}))
 await screen.findByText('Synthetic export failure')
 expect(screen.getByRole('button',{name:'Export My Data'})).toBeEnabled()
 expect(screen.queryByText('Data exported successfully')).not.toBeInTheDocument()
})
