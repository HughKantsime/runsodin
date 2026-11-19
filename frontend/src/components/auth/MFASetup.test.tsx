import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import MFASetup from './MFASetup'
const api=vi.hoisted(()=>({status:vi.fn(),remaining:vi.fn().mockResolvedValue({remaining:10}),setup:vi.fn(),confirm:vi.fn(),disable:vi.fn()}))
vi.mock('../../api',()=>({auth:{mfaStatus:api.status,recoveryStatus:()=>api.remaining(),mfaSetup:api.setup,mfaConfirm:api.confirm,mfaDisable:api.disable}}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
test('unknown MFA status does not present the account as unenrolled',async()=>{
  api.status.mockRejectedValue(new Error('Synthetic status unavailable'))
  render(<MFASetup />)
  await waitFor(()=>expect(api.status).toHaveBeenCalledOnce())
  await screen.findByText('Unable to load MFA status. Retry before making changes.')
  expect(screen.queryByRole('button',{name:'Set Up Two-Factor Authentication'})).not.toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'Disable MFA'})).not.toBeInTheDocument()
  expect(api.setup).not.toHaveBeenCalled()
  expect(screen.getByRole('button',{name:'Retry MFA status'})).toBeEnabled()
})
test('confirmed enabled status presents the protected account',async()=>{
  api.status.mockResolvedValue({mfa_enabled:true})
  render(<MFASetup />)
  expect(await screen.findByText('Your account is protected with two-factor authentication.')).toBeVisible()
  expect(screen.getByRole('button',{name:'Disable MFA'})).toBeDisabled()
})
test('confirmed unenrolled status allows explicit setup with QR then confirmation',async()=>{
  api.status.mockResolvedValue({mfa_enabled:false})
  api.setup.mockResolvedValue({secret:'synthetic-not-a-real-mfa-secret',qr_code:'data:image/png;base64,c3ludGhldGlj'})
  api.confirm.mockResolvedValue({recovery_codes:['example-code']})
  render(<MFASetup />)
  fireEvent.click(await screen.findByRole('button',{name:'Set Up Two-Factor Authentication'}))
  expect(await screen.findByAltText('MFA QR Code')).toBeVisible()
  const input=screen.getByPlaceholderText('000000')
  fireEvent.change(input,{target:{value:'123456'}})
  fireEvent.click(screen.getByRole('button',{name:'Confirm'}))
  await screen.findByText('MFA enabled successfully')
  expect(api.confirm).toHaveBeenCalledExactlyOnceWith('123456')
  expect(screen.queryByAltText('MFA QR Code')).not.toBeInTheDocument()
})

for(const enabled of [true,false])test(`status retry accepts confirmed ${enabled} state and immediate duplicate retry sends one request`,async()=>{
  let finish!:(value:any)=>void
  api.status.mockRejectedValueOnce(new Error('Synthetic unavailable'))
    .mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
  render(<MFASetup />)
  const retry=await screen.findByRole('button',{name:'Retry MFA status'})
  fireEvent.click(retry);fireEvent.click(retry)
  await waitFor(()=>expect(api.status).toHaveBeenCalledTimes(2))
  expect(screen.getByRole('status')).toHaveTextContent('Loading MFA status')
  expect(screen.queryByRole('button',{name:'Set Up Two-Factor Authentication'})).not.toBeInTheDocument()
  finish({mfa_enabled:enabled})
  if(enabled)await screen.findByText('Your account is protected with two-factor authentication.')
  else await screen.findByRole('button',{name:'Set Up Two-Factor Authentication'})
})
test('malformed status is unknown rather than enabled or disabled',async()=>{
  api.status.mockResolvedValue({mfa_enabled:'false'})
  render(<MFASetup />)
  await screen.findByRole('button',{name:'Retry MFA status'})
  expect(screen.queryByText('Enabled',{exact:true})).not.toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'Set Up Two-Factor Authentication'})).not.toBeInTheDocument()
})
test('previous principal status response cannot replace the current principal status',async()=>{
  let old!:(value:any)=>void
  api.status.mockImplementationOnce(()=>new Promise(resolve=>{old=resolve}))
    .mockResolvedValueOnce({mfa_enabled:true})
  const view=render(<MFASetup principalKey={1}/>)
  view.rerender(<MFASetup principalKey={2}/>)
  await screen.findByText('Your account is protected with two-factor authentication.')
  await act(async()=>{old({mfa_enabled:false})})
  await waitFor(()=>expect(api.status).toHaveBeenCalledTimes(2))
  expect(screen.getByText('Your account is protected with two-factor authentication.')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Set Up Two-Factor Authentication'})).not.toBeInTheDocument()
})
test('unmounted pending status does not affect a new mount',async()=>{
  let old!:(value:any)=>void
  api.status.mockImplementationOnce(()=>new Promise(resolve=>{old=resolve}))
    .mockResolvedValueOnce({mfa_enabled:true})
  const previous=render(<MFASetup/>);previous.unmount()
  render(<MFASetup/>)
  await screen.findByText('Your account is protected with two-factor authentication.')
  await act(async()=>{old({mfa_enabled:false})})
  expect(screen.getByText('Your account is protected with two-factor authentication.')).toBeVisible()
})

test.each([0, 1, 3, 4])('shows the recovery warning at three or fewer codes (%i remaining)', async remaining => {
  api.status.mockResolvedValue({mfa_enabled: true})
  api.remaining.mockResolvedValueOnce({remaining})
  render(<MFASetup />)
  expect(await screen.findByText(`${remaining} recovery ${remaining === 1 ? 'code' : 'codes'} remaining.`)).toBeVisible()
  expect(screen.queryByText(/Generate a new set while you still have access/)!== null).toBe(remaining <= 3)
  expect(screen.getByRole('button', {name:'Regenerate recovery codes'})).toBeEnabled()
})

test.each(['status', 'remaining', 'setup', 'confirm'] as const)(
  'renders nothing during the login redirect from %s', async operation => {
    api.status.mockResolvedValueOnce(operation === 'status' ? undefined : {mfa_enabled: operation === 'remaining'})
    if (operation === 'remaining') api.remaining.mockResolvedValueOnce(undefined)
    if (operation === 'setup') api.setup.mockResolvedValueOnce(undefined)
    if (operation === 'confirm') {
      api.setup.mockResolvedValueOnce({qr_code: 'data:image/png;base64,c3ludGhldGlj'})
      api.confirm.mockResolvedValueOnce(undefined)
    }
    const {container} = render(<MFASetup />)
    if (operation === 'setup' || operation === 'confirm') {
      fireEvent.click(await screen.findByRole('button', {name: 'Set Up Two-Factor Authentication'}))
    }
    if (operation === 'confirm') {
      fireEvent.change(await screen.findByLabelText('Verification code'), {target: {value: '123456'}})
      fireEvent.click(screen.getByRole('button', {name: 'Confirm'}))
    }
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(api[operation]).toHaveBeenCalledOnce()
  }
)
