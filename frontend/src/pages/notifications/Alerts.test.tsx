import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {MemoryRouter} from 'react-router-dom'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import Alerts from './Alerts'
import {alerts as api, fetchAPI, printers} from '../../api'
import {getCurrentUser} from '../../permissions'
import type {AlertType} from '../../types/notification'
import toast from 'react-hot-toast'
vi.mock('../../api',()=>({alerts:{list:vi.fn(),remove:vi.fn(),markRead:vi.fn(),markAllRead:vi.fn(),dismiss:vi.fn()},fetchAPI:vi.fn(async()=>({user_digest:null})),approveJob:vi.fn(),rejectJob:vi.fn(),printers:{list:vi.fn()}}))
vi.mock('../../permissions',()=>({getCurrentUser:vi.fn()}))
const row={id:701,title:'Synthetic retained alert',message:'Synthetic details',severity:'info',alert_type:'print_complete',is_read:false,created_at:'2026-10-11T12:00:00'}
function show(){render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><Alerts/></MemoryRouter></QueryClientProvider>)}
beforeEach(()=>{vi.mocked(api.list).mockResolvedValue([row] as any);vi.mocked(getCurrentUser).mockReturnValue({username:'Synthetic operator',role:'operator'});vi.mocked(api.remove).mockResolvedValue({status:"ok",alert_id:701})})
afterEach(()=>{cleanup();vi.clearAllMocks()})
test.each(['printer_error', 'PRINTER_ERROR'])('shows the server’s %s alert in the list', async (alertType) => {
 const alert = {
   ...row, user_id:1, alert_type:alertType as AlertType, severity:'warning' as const,
   title:'Printer command not confirmed', message:'Check the printer before trying again.',
   is_dismissed:false, printer_id:9, printer_name:'Printer 9', job_id:null, job_name:null,
   spool_id:null, spool_name:null, metadata_json:{kind:'control_timeout'},
 }
 vi.mocked(api.list).mockResolvedValue([alert])
 show()
 expect(await screen.findByText(alert.title)).toBeVisible()
 expect(screen.getByText(alert.message)).toBeVisible()
})

test('confirmed deletion removes row and refreshes unread consumers',async()=>{
 const changed=vi.fn();window.addEventListener('odin:alerts-changed',changed)
 try {show();await screen.findByText(row.title);fireEvent.click(screen.getByRole('button',{name:'Delete alert'}));await waitFor(()=>expect(screen.queryByText(row.title)).toBeNull());expect(api.remove).toHaveBeenCalledWith(701);expect(changed).toHaveBeenCalledTimes(1)}
 finally {window.removeEventListener('odin:alerts-changed',changed)}
})
test('pending deletion retains row and prevents repeated submission',async()=>{
 let resolve:any;vi.mocked(api.remove).mockImplementation(()=>new Promise(done=>{resolve=done}));show();await screen.findByText(row.title);const button=screen.getByRole('button',{name:'Delete alert'});fireEvent.click(button);fireEvent.click(button);expect(button).toBeDisabled();expect(screen.getByText(row.title)).toBeTruthy();expect(api.remove).toHaveBeenCalledTimes(1);resolve({status:"ok",alert_id:701});await waitFor(()=>expect(screen.queryByText(row.title)).toBeNull())
})
test('failed deletion preserves row and allows explicit retry',async()=>{
 vi.mocked(api.remove).mockRejectedValue(new Error('Synthetic refusal'));const notify=vi.spyOn(toast,'error');show();await screen.findByText(row.title);fireEvent.click(screen.getByRole('button',{name:'Delete alert'}));await waitFor(()=>expect(notify).toHaveBeenCalledWith('Failed to delete alert'));expect(screen.getByText(row.title)).toBeTruthy();expect(screen.getByRole('button',{name:'Delete alert'})).not.toBeDisabled();notify.mockRestore()
})
test('viewer cannot see delete action',async()=>{
 vi.mocked(getCurrentUser).mockReturnValue({username:'Synthetic viewer',role:'viewer'});show();await screen.findByText(row.title);expect(screen.queryByRole('button',{name:'Delete alert'})).toBeNull()
})

test('unconfirmed server result preserves row',async()=>{
 vi.mocked(api.remove).mockResolvedValue(undefined as any);const notify=vi.spyOn(toast,'error');show();await screen.findByText(row.title);fireEvent.click(screen.getByRole('button',{name:'Delete alert'}));await waitFor(()=>expect(notify).toHaveBeenCalledWith('Failed to delete alert'));expect(screen.getByText(row.title)).toBeTruthy();notify.mockRestore()
})
test('late pre-delete list response cannot restore deleted row',async()=>{
 let resolve:any;show();await screen.findByText(row.title);vi.mocked(api.list).mockImplementationOnce(()=>new Promise(done=>{resolve=done}));fireEvent.click(screen.getByRole('button',{name:'Info'}));await waitFor(()=>expect(resolve).toBeTruthy());fireEvent.click(screen.getByRole('button',{name:'Delete alert'}));await waitFor(()=>expect(screen.queryByText(row.title)).toBeNull());await act(async()=>resolve([row]));await waitFor(()=>expect(screen.queryByText('Loading alerts...')).toBeNull());expect(screen.queryByText(row.title)).toBeNull()
})

test('delete finishing during a new filter request does not discard its rows',async()=>{
 let deleted:any,list:any;vi.mocked(api.remove).mockImplementation(()=>new Promise(done=>{deleted=done}));show();await screen.findByText(row.title);fireEvent.click(screen.getByRole('button',{name:'Delete alert'}));vi.mocked(api.list).mockImplementationOnce(()=>new Promise(done=>{list=done}));fireEvent.click(screen.getByRole('button',{name:'Warning'}));await waitFor(()=>expect(list).toBeTruthy());await act(async()=>deleted({status:'ok',alert_id:701}));await act(async()=>list([{...row,id:702,title:'Synthetic warning',severity:'warning'}]));expect(screen.getByText('Synthetic warning')).toBeTruthy();expect(screen.queryByText(row.title)).toBeNull()
})


test('restart question alert opens the printer recovery question', async () => {
 vi.mocked(api.list).mockResolvedValue([{...row, printer_id:9, metadata_json:{restart_question_id:'a'.repeat(32)}}] as any)
 show()
 await screen.findByText(row.title)
 expect(screen.getByRole('link', {name:'Confirm print in Dispatch Recovery'})).toHaveAttribute('href','/printers/9#dispatch-recovery')
})

test('vision alert shows the resume receipt and follows pending printer state', async () => {
 const printer = {id:9, pending_control:{action:'pause', sent_at:new Date().toISOString(), deadline_at:new Date(Date.now()+30000).toISOString()}}
 vi.mocked(printers.list).mockResolvedValue([printer] as any)
 vi.mocked(api.list).mockResolvedValue([{...row, alert_type:'spaghetti_detected', printer_id:9}] as any)
 vi.mocked(fetchAPI).mockImplementation(async path => {
   if (path !== '/printers/9/resume') return {}
   printer.pending_control.action = 'resume'
   return {success:true, outcome:'pending', message:'Resume sent. The printer is reheating.'}
 })
 const notify = vi.spyOn(toast,'success')
 try {
   show()
   await screen.findByText('Pause sent, waiting for the printer')
   fireEvent.click(screen.getByRole('button',{name:/false alarm.*resume/i}))
   await waitFor(() => expect(notify).toHaveBeenCalledWith('Resume sent. The printer is reheating.'))
   expect(await screen.findByText('Resume sent, waiting for the printer')).toBeInTheDocument()
 } finally { notify.mockRestore() }
})

test('allows a vision alert resume after its pending deadline', async () => {
 vi.mocked(printers.list).mockResolvedValue([{id:9, pending_control:{action:'resume', deadline_at: new Date(0).toISOString()}}] as any)
 vi.mocked(api.list).mockResolvedValue([{...row, alert_type:'spaghetti_detected', printer_id:9}] as any)
 show()
 await waitFor(() => expect(printers.list).toHaveBeenCalled())
 expect(await screen.findByRole('button', {name:/false alarm.*resume/i})).toBeEnabled()
 expect(screen.queryByText('Resume sent, waiting for the printer')).toBeNull()
})
