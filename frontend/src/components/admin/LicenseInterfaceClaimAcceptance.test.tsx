import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
function render(node: React.ReactNode) { const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], { id:3, role:'admin', group_id:1 }); return rtlRender(<QueryClientProvider client={client}>{node}</QueryClientProvider>) }
import LicenseTab from './LicenseTab'
import ProGate from '../shared/ProGate'
import { LicenseProvider, useLicense } from '../../LicenseContext'
const api=vi.hoisted(()=>({fetch:vi.fn(),get:vi.fn(),upload:vi.fn()}))
vi.mock('../../api',()=>({fetchAPI:api.fetch,license:{get:api.get,upload:api.upload},downloadBlob:vi.fn()}))
beforeEach(()=>{api.fetch.mockResolvedValue({tier:'community',features:[]});api.get.mockResolvedValue({tier:'community',managed_externally:false});api.upload.mockResolvedValue({tier:'pro'})})
afterEach(()=>{cleanup();vi.clearAllMocks()})
function Refresh(){const lic=useLicense();return <button onClick={lic.refresh}>Refresh entitlement</button>}
function gated(){return render(<MemoryRouter initialEntries={['/gated']}><LicenseProvider><Refresh/><Routes><Route path="/gated" element={<ProGate feature="print_quotas"><button>Protected quota action</button></ProGate>}/><Route path="/" element={<h1>Synthetic Dashboard</h1>}/></Routes></LicenseProvider></MemoryRouter>)}
test('F20-20.2-03 missing feature blocks protected UI and Back reaches dashboard',async()=>{
 gated();await screen.findByRole('heading',{name:'Pro Feature'})
 expect(screen.queryByRole('button',{name:'Protected quota action'})).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Back to Dashboard'}))
 expect(screen.getByRole('heading',{name:'Synthetic Dashboard'})).toBeInTheDocument()
})
test('F20-20.2-03 backend feature entitlement unlocks child even for Community tier',async()=>{
 api.get.mockResolvedValue({tier:'community',features:['print_quotas']});gated()
 await screen.findByRole('button',{name:'Protected quota action'})
 expect(screen.queryByRole('heading',{name:'Pro Feature'})).not.toBeInTheDocument()
})
test('F20-20.2-03 Pro tier alone does not grant absent feature',async()=>{
 api.get.mockResolvedValue({tier:'pro',features:[]});gated()
 await screen.findByRole('heading',{name:'Pro Feature'})
 expect(screen.queryByRole('button',{name:'Protected quota action'})).not.toBeInTheDocument()
})
test('F20-20.2-03 failed refresh removes previously entitled child',async()=>{
 api.get.mockResolvedValueOnce({tier:'education',features:['print_quotas']}).mockRejectedValueOnce(new Error('Synthetic unavailable'));gated()
 await screen.findByRole('button',{name:'Protected quota action'})
 fireEvent.click(screen.getByRole('button',{name:'Refresh entitlement'}))
 await screen.findByRole('heading',{name:'Pro Feature'})
 expect(screen.queryByRole('button',{name:'Protected quota action'})).not.toBeInTheDocument()
})
test('F20-20.2-04 upload sends selected license file and refreshes displayed tier',async()=>{
 const view=render(<LicenseTab/>);await screen.findByText('Click to select .license file')
 api.get.mockResolvedValue({tier:'pro',managed_externally:false})
 const file=new File(['synthetic unsigned test fixture'],'synthetic.license')
 const input=view.container.querySelector('input[type=file]')!
 expect(input).toHaveAttribute('accept','.license')
 fireEvent.change(input,{target:{files:[file]}})
 await waitFor(()=>expect(api.upload).toHaveBeenCalledExactlyOnceWith(file))
 await screen.findByText('License activated: pro tier')
 await screen.findByText('pro', { selector:'span' })
 expect(api.get).toHaveBeenCalledTimes(2)
 expect(input).toHaveValue('')
})
test('F20-20.2-04 rejected upload displays failure, resets file input and avoids refresh/success',async()=>{
 api.upload.mockRejectedValue(new Error('Synthetic rejected file'))
 const view=render(<LicenseTab/>);await screen.findByText('Click to select .license file')
 const input=view.container.querySelector('input[type=file]')!
 fireEvent.change(input,{target:{files:[new File(['synthetic'],'invalid.license')]}})
 await screen.findByText('Upload failed: Synthetic rejected file')
 expect(api.get).toHaveBeenCalledOnce()
 expect(screen.queryByText('License activated: pro tier')).not.toBeInTheDocument()
 expect(input).toHaveValue('')
})
test('F20-20.2-04 no selected file issues no upload',async()=>{
 const view=render(<LicenseTab/>);await screen.findByText('Click to select .license file')
 fireEvent.change(view.container.querySelector('input[type=file]')!,{target:{files:[]}})
 expect(api.upload).not.toHaveBeenCalled()
})
test('F20-20.2-04 externally managed license hides upload controls',async()=>{
 api.get.mockResolvedValue({tier:'education',managed_externally:true})
 const view=render(<LicenseTab/>)
 await screen.findByRole('heading',{name:'Externally Managed License'})
 expect(view.container.querySelector('input[type=file]')).toBeNull()
 expect(api.upload).not.toHaveBeenCalled()
})
