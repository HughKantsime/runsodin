import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import Admin from './Admin'
const api=vi.hoisted(()=>({reset:vi.fn(),success:vi.fn(),error:vi.fn()}))
vi.mock('../../api',()=>({auth:{adminDisableMfa:api.reset},groups:{list:vi.fn()},users:{resetPasswordEmail:vi.fn()}}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({hasFeature:()=>false,atUserLimit:()=>false,maxUsers:0})}))
vi.mock('react-hot-toast',()=>({default:{success:api.success,error:api.error}}))
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.clearAllMocks()})
function mount(secondEnabled=false){
  let reads=0
  vi.stubGlobal('fetch',vi.fn(async()=>{reads++;return {ok:true,json:async()=>[
    {id:6,username:'Synthetic enrolled',role:'viewer',is_active:true,mfa_enabled:true},
    {id:7,username:'Synthetic unenrolled',role:'viewer',is_active:true,mfa_enabled:secondEnabled},
  ]}}))
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
  render(<QueryClientProvider client={client}><Admin /></QueryClientProvider>)
  return ()=>reads
}
test('MFA recovery requires confirmation and cancel leaves state untouched',async()=>{
  mount()
  fireEvent.click(await screen.findByRole('button',{name:'Reset MFA for Synthetic enrolled'}))
  expect(screen.queryByRole('button',{name:'Reset MFA for Synthetic unenrolled'})).not.toBeInTheDocument()
  expect(api.reset).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Cancel'}))
  expect(screen.queryByRole('heading',{name:'Reset MFA'})).not.toBeInTheDocument()
  expect(api.reset).not.toHaveBeenCalled()
})
test('immediate duplicate confirmation issues one fixed-target request and pending controls disable',async()=>{
  let finish!:()=>void
  api.reset.mockImplementation(()=>new Promise<void>(resolve=>{finish=resolve}))
  const reads=mount()
  fireEvent.click(await screen.findByRole('button',{name:'Reset MFA for Synthetic enrolled'}))
  const button=screen.getByRole('button',{name:'Disable MFA'})
  fireEvent.click(button);fireEvent.click(button)
  await waitFor(()=>expect(api.reset).toHaveBeenCalledExactlyOnceWith(6))
  expect(button).toBeDisabled()
  expect(screen.getByRole('button',{name:'Cancel'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Reset MFA for Synthetic enrolled'})).toBeDisabled()
  finish()
  await waitFor(()=>expect(api.success).toHaveBeenCalled())
  expect(reads()).toBeGreaterThan(1)
  expect(screen.queryByRole('heading',{name:'Reset MFA'})).not.toBeInTheDocument()
})
test('uncertain reset closes confirmation and refetches current state without retry',async()=>{
  api.reset.mockRejectedValue(new Error('Synthetic commit uncertainty'))
  const reads=mount()
  fireEvent.click(await screen.findByRole('button',{name:'Reset MFA for Synthetic enrolled'}))
  fireEvent.click(screen.getByRole('button',{name:'Disable MFA'}))
  await waitFor(()=>expect(api.error).toHaveBeenCalledWith(expect.stringContaining('Check the user list before retrying')))
  expect(api.reset).toHaveBeenCalledExactlyOnceWith(6)
  expect(reads()).toBeGreaterThan(1)
  expect(screen.queryByRole('heading',{name:'Reset MFA'})).not.toBeInTheDocument()
})

test('cancel then select another user binds confirmation to the new ID',async()=>{
  api.reset.mockResolvedValue(undefined)
  mount(true)
  fireEvent.click(await screen.findByRole('button',{name:'Reset MFA for Synthetic enrolled'}))
  fireEvent.click(screen.getByRole('button',{name:'Cancel'}))
  fireEvent.click(screen.getByRole('button',{name:'Reset MFA for Synthetic unenrolled'}))
  expect(screen.getByText(/Disable MFA for "Synthetic unenrolled"/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Disable MFA'}))
  await waitFor(()=>expect(api.reset).toHaveBeenCalledExactlyOnceWith(7))
})
