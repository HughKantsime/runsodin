import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { afterEach,expect,test,vi } from 'vitest'
import { QueryClient,QueryClientProvider } from '@tanstack/react-query'
import Admin from './Admin'
const notice=vi.hoisted(()=>({warning:vi.fn(),error:vi.fn(),success:vi.fn()}))
vi.mock('react-hot-toast',()=>({default:Object.assign(notice.warning,{error:notice.error,success:notice.success})}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({hasFeature:()=>false,atUserLimit:()=>false,maxUsers:0})}))
vi.mock('../../api',()=>({groups:{list:vi.fn()},users:{resetPasswordEmail:vi.fn()}}))
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.clearAllMocks()})

async function submit(welcome=true){
  await screen.findByText('No users found')
  fireEvent.click(screen.getByRole('button',{name:'Add User'}))
  fireEvent.change(screen.getByText('Username').parentElement!.querySelector('input')!,{target:{value:'Synthetic Created'}})
  fireEvent.change(screen.getByText('Email').parentElement!.querySelector('input')!,{target:{value:'synthetic@example.invalid'}})
  if(welcome)fireEvent.click(screen.getByLabelText('Send welcome email with auto-generated password'))
  else fireEvent.change(screen.getByPlaceholderText('Min 8 characters'),{target:{value:crypto.randomUUID()+'aA1'}})
  fireEvent.click(screen.getByRole('button',{name:'Create User'}))
}

function mount(result:any,ok=true){
  let posts=0,reads=0
  vi.stubGlobal('fetch',vi.fn(async(_url:any,options:any)=>{
    if(options?.method==='POST'){
      posts++;return {ok,status:ok?200:503,json:async()=>result}
    }
    reads++
    return {ok:true,json:async()=>posts?[{id:6,username:'Synthetic Created',email:'synthetic@example.invalid',role:'viewer',is_active:true}]:[]}
  }))
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
  render(<QueryClientProvider client={client}><Admin /></QueryClientProvider>)
  return {posts:()=>posts,reads:()=>reads}
}

test('created account with unconfirmed mail closes form, refreshes users and explains next action',async()=>{
  const message='Account created, but welcome email delivery was not confirmed. Check email settings, then use Send password reset link for this user.'
  const calls=mount({status:'created',welcome_email:'unconfirmed',message})
  await submit()
  await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith(message,expect.objectContaining({duration:8000})))
  await screen.findByText('Synthetic Created')
  expect(screen.queryByRole('heading',{name:'New User'})).not.toBeInTheDocument()
  expect(notice.error).not.toHaveBeenCalled()
  expect(calls.posts()).toBe(1)
  expect(calls.reads()).toBeGreaterThan(1)
})

for(const welcome of ['sent',undefined])test(`confirmed normal create ${welcome??'without mail'} does not show uncertain-delivery warning`,async()=>{
  const calls=mount({status:'created',...(welcome?{welcome_email:welcome}:{})})
  await submit(Boolean(welcome))
  await screen.findByText('Synthetic Created')
  expect(screen.queryByRole('heading',{name:'New User'})).not.toBeInTheDocument()
  expect(notice.warning).not.toHaveBeenCalled()
  expect(notice.error).not.toHaveBeenCalled()
  expect(calls.posts()).toBe(1)
})

test('unconfirmed database creation shows error and refreshes visible users without retrying creation',async()=>{
  const detail='Account creation could not be confirmed. Check the user list before retrying.'
  const calls=mount({detail},false)
  await submit()
  await waitFor(()=>expect(notice.error).toHaveBeenCalledWith(detail))
  await screen.findByText('Synthetic Created')
  expect(screen.getByRole('heading',{name:'New User'})).toBeInTheDocument()
  expect(calls.posts()).toBe(1)
  expect(calls.reads()).toBeGreaterThan(1)
})


test('legacy or malformed welcome success does not imply confirmed email delivery',async()=>{
  const calls=mount({status:'created'})
  await submit(true)
  await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith(expect.stringContaining('welcome email delivery was not confirmed'),expect.anything()))
  await screen.findByText('Synthetic Created')
  expect(calls.posts()).toBe(1)
})
