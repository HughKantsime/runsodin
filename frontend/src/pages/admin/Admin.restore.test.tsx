import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Admin from './Admin'
const api=vi.hoisted(()=>({me:vi.fn()}))
vi.mock('../../api',()=>({auth:{me:api.me},groups:{list:vi.fn()},users:{restoreSuperadmin:vi.fn()}}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({hasFeature:()=>false,atUserLimit:()=>false,maxUsers:0})}))
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.clearAllMocks()})
function mount(group_id:number|null) {
  api.me.mockResolvedValue({id:1,role:'admin',group_id})
  vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>[
    {id:2,username:'stranded',role:'admin',group_id:1,is_active:true},
    {id:3,username:'student',role:'viewer',group_id:1,is_active:true},
    {id:4,username:'platform',role:'admin',group_id:null,is_active:true},
  ]})))
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><Admin /></QueryClientProvider>)
}
test('platform admin gets restore action only for organization admins',async()=>{
  mount(null)
  expect(await screen.findByRole('button',{name:'Restore platform Super Admin for stranded'})).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'Restore platform Super Admin for student'})).not.toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'Restore platform Super Admin for platform'})).not.toBeInTheDocument()
})
test('school admins do not get restore actions',async()=>{
  mount(1)
  await screen.findByText('stranded')
  await waitFor(()=>expect(api.me).toHaveBeenCalled())
  expect(screen.queryByRole('button',{name:/Restore platform Super Admin/})).not.toBeInTheDocument()
})
