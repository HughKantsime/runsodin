import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import MaintenanceBanner from './MaintenanceBanner'
import { printerMaintenance } from '../../api/printerMaintenance'
vi.mock('../../api/printerMaintenance',()=>({printerMaintenance:{get:vi.fn(),set:vi.fn()}}))
const warning={state:'warn' as const,note:'Part fan failing; replacement ordered',set_by:3,set_by_name:'Shane',set_at:'2026-10-27T12:00:00Z'}
beforeEach(()=>{vi.clearAllMocks();vi.mocked(printerMaintenance.get).mockResolvedValue(warning);vi.mocked(printerMaintenance.set).mockResolvedValue({...warning,state:'ok',note:''})})
function mount(role='viewer') {
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});client.setQueryData(['auth','me'],{role})
 render(<QueryClientProvider client={client}><MaintenanceBanner printerId={9} editable/></QueryClientProvider>)
}
it('shows the warning and actor to viewers without an edit action',async()=>{
 mount();expect(await screen.findByRole('status')).toHaveTextContent('Maintenance warning')
 expect(screen.getByRole('status')).toHaveTextContent(warning.note)
 expect(screen.getByRole('status')).toHaveTextContent('Shane')
 expect(screen.queryByText('Set maintenance state')).not.toBeInTheDocument()
})
it('shows a hold and allows an administrator to clear it',async()=>{
 vi.mocked(printerMaintenance.get).mockResolvedValue({...warning,state:'hold'})
 mount('admin');expect(await screen.findByRole('status')).toHaveTextContent('dispatch blocked')
 fireEvent.click(screen.getByText('Set maintenance state'))
 fireEvent.change(screen.getByLabelText('Maintenance state'),{target:{value:'ok'}})
 fireEvent.change(screen.getByLabelText('Maintenance note'),{target:{value:''}})
 fireEvent.click(screen.getByText('Save maintenance'))
 await waitFor(()=>expect(printerMaintenance.set).toHaveBeenCalledWith(9,{state:'ok',note:''}))
 await waitFor(()=>expect(screen.queryByRole('status')).not.toBeInTheDocument())
})
it('reports read failure without claiming the printer is OK',async()=>{
 vi.mocked(printerMaintenance.get).mockRejectedValue(new Error('Unavailable'));mount()
 expect(await screen.findByRole('alert')).toHaveTextContent('Maintenance status unavailable')
})

it('uses printer-list maintenance without a per-card fetch and updates with the payload',()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}})
 const {rerender}=render(<QueryClientProvider client={client}><MaintenanceBanner printerId={9} maintenance={warning}/></QueryClientProvider>)
 expect(screen.getByRole('status')).toHaveTextContent(warning.note)
 expect(printerMaintenance.get).not.toHaveBeenCalled()
 rerender(<QueryClientProvider client={client}><MaintenanceBanner printerId={9} maintenance={{...warning,state:'hold'}}/></QueryClientProvider>)
 expect(screen.getByRole('status')).toHaveTextContent('dispatch blocked')
 expect(printerMaintenance.get).not.toHaveBeenCalled()
})
