import {act,cleanup,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import {MemoryRouter} from 'react-router-dom'
import AlertBell from './AlertBell'
import {alerts} from '../../api'
vi.mock('../../api',()=>({alerts:{unreadCount:vi.fn(),list:vi.fn(),markRead:vi.fn(),markAllRead:vi.fn()}}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
test('confirmed alert change refreshes badge and unmount removes listener',async()=>{
 vi.mocked(alerts.unreadCount).mockResolvedValueOnce({unread_count:2}).mockResolvedValueOnce({unread_count:1})
 const view=render(<MemoryRouter><AlertBell/></MemoryRouter>)
 await screen.findByRole('button',{name:'Notifications, 2 unread'})
 act(()=>window.dispatchEvent(new Event('odin:alerts-changed')))
 await screen.findByRole('button',{name:'Notifications, 1 unread'})
 expect(alerts.unreadCount).toHaveBeenCalledTimes(2)
 view.unmount();act(()=>window.dispatchEvent(new Event('odin:alerts-changed')))
 expect(alerts.unreadCount).toHaveBeenCalledTimes(2)
})
test('late old count response cannot overwrite post-change badge',async()=>{
 let resolve:any;vi.mocked(alerts.unreadCount).mockImplementationOnce(()=>new Promise(done=>{resolve=done})).mockResolvedValueOnce({unread_count:1})
 render(<MemoryRouter><AlertBell/></MemoryRouter>);await waitFor(()=>expect(resolve).toBeTruthy())
 act(()=>window.dispatchEvent(new Event('odin:alerts-changed')));await screen.findByRole('button',{name:'Notifications, 1 unread'})
 await act(async()=>resolve({unread_count:2}));expect(screen.getByRole('button',{name:'Notifications, 1 unread'})).toBeTruthy()
})
