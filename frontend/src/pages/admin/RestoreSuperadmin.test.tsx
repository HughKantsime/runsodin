import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RestoreSuperadmin from './RestoreSuperadmin'
const api = vi.hoisted(() => ({ restoreSuperadmin: vi.fn() }))
vi.mock('../../api', () => ({ users: api }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{mutations:{retry:false}}})}><RestoreSuperadmin user={{id:2,username:'stranded'}} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Restore platform Super Admin for stranded' }))
}
test('exact typed confirmation is required; cancel performs no write', () => {
  mount()
  expect(screen.getByRole('button',{name:'Restore account'})).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Confirm target username'),{target:{value:'Stranded'}})
  expect(screen.getByRole('button',{name:'Restore account'})).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'Cancel'}))
  expect(api.restoreSuperadmin).not.toHaveBeenCalled()
})
test('one request with typed username, controls disabled while pending', async () => {
  let finish!: (result: {status:string}) => void
  api.restoreSuperadmin.mockImplementation(() => new Promise(resolve => { finish=resolve }))
  mount()
  fireEvent.change(screen.getByLabelText('Confirm target username'),{target:{value:'stranded'}})
  const submit=screen.getByRole('button',{name:'Restore account'})
  fireEvent.click(submit);fireEvent.click(submit)
  await waitFor(() => expect(api.restoreSuperadmin).toHaveBeenCalledExactlyOnceWith(2,'stranded'))
  expect(screen.getByRole('button',{name:'Cancel'})).toBeDisabled()
  finish({status:'restored'})
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
test('live work refusal remains visible and can be retried explicitly', async () => {
  api.restoreSuperadmin.mockRejectedValue(new Error('Resolve live Education work'))
  mount()
  fireEvent.change(screen.getByLabelText('Confirm target username'),{target:{value:'stranded'}})
  fireEvent.click(screen.getByRole('button',{name:'Restore account'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Resolve live Education work')
  expect(api.restoreSuperadmin).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button',{name:'Restore account'})).toBeEnabled()
})
