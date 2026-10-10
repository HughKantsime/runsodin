import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TroubleshootingPanel from './TroubleshootingPanel'
const api=vi.hoisted(()=>({fetchAPI:vi.fn()}))
vi.mock('../../api',()=>api)
const result={diagnostic_id:'test-diagnostic',checked_at:'2026-10-10T20:00:00Z',checks:[{stage:'ftps_auth',status:'fail',message:'Printer authentication was rejected.',next_step:'Check saved access code.',details:{}}]}
beforeEach(()=>{api.fetchAPI.mockReset();api.fetchAPI.mockImplementation((path)=>Promise.resolve(path==='/printers'?[{id:2,name:'Test printer',api_type:'bambu'},{id:3,name:'Other printer',api_type:'moonraker'}]:result))})
afterEach(cleanup)
it('requires saved printer and explicit transfer confirmation',async()=>{
 render(<TroubleshootingPanel/>);expect(screen.getByRole('button',{name:'Test file transfer'})).toBeDisabled()
 await screen.findByRole('option',{name:'Test printer'});fireEvent.change(screen.getByLabelText('Printer'),{target:{value:'2'}})
 expect(screen.getByRole('button',{name:'Test file transfer'})).toBeDisabled()
 fireEvent.click(screen.getByLabelText(/I authorize/));fireEvent.click(screen.getByRole('button',{name:'Test file transfer'}))
 await screen.findByText('Printer authentication was rejected.')
 expect(api.fetchAPI).toHaveBeenCalledWith('/system/troubleshooting/printer',expect.objectContaining({body:JSON.stringify({printer_id:2,transfer:true,confirm_transfer:true})}))
 expect(screen.getByText(/Next: Check saved access code/)).toBeInTheDocument()
})
it('does not claim unsupported transfer and clears stale selected-printer evidence',async()=>{
 render(<TroubleshootingPanel/>);await screen.findByRole('option',{name:'Other printer'});fireEvent.change(screen.getByLabelText('Printer'),{target:{value:'3'}})
 expect(screen.getByRole('button',{name:'Test file transfer'})).toBeDisabled();expect(screen.getByText(/safe transfer test is not available/)).toBeInTheDocument()
})
it('previews report before download or opening a user-controlled email draft',async()=>{
 render(<TroubleshootingPanel/>);fireEvent.change(screen.getByLabelText(/What happened/),{target:{value:'Transfer failed'}})
 fireEvent.click(screen.getByRole('button',{name:'Preview support report'}))
 await screen.findByLabelText('Support report preview')
 expect(api.fetchAPI).toHaveBeenCalledWith('/system/troubleshooting/report',expect.objectContaining({body:JSON.stringify({problem:'Transfer failed',include_logs:false})}))
 expect(screen.getByRole('link',{name:'Open email draft'})).toHaveAttribute('href',expect.stringContaining('mailto:support@runsodin.com'))
 expect(screen.getByText(/does not attach a file or send anything/)).toBeInTheDocument()
 fireEvent.change(screen.getByLabelText(/What happened/),{target:{value:'Changed description'}})
 expect(screen.queryByLabelText('Support report preview')).not.toBeInTheDocument()
})
it('exposes request failure and restores controls',async()=>{
 api.fetchAPI.mockImplementation(path=>path==='/printers'?Promise.resolve([]):Promise.reject(new Error('Server unavailable')))
 render(<TroubleshootingPanel/>);fireEvent.click(screen.getByRole('button',{name:'Check server'}))
 await screen.findByRole('alert');expect(screen.getByRole('alert')).toHaveTextContent('Server unavailable')
 await waitFor(()=>expect(screen.getByRole('button',{name:'Check server'})).toBeEnabled())
})
