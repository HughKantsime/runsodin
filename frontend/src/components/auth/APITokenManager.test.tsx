import React from 'react'
import { act, cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import APITokenManager from './APITokenManager'
import { apiTokens, getEducationMode } from '../../api'
import { getCurrentUser } from '../../permissions'
import { useLicense } from '../../LicenseContext'
vi.mock('../../permissions', () => ({ getCurrentUser: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: vi.fn() }))
vi.mock('../../api', () => ({ getEducationMode: vi.fn(), apiTokens: { list: vi.fn(), create: vi.fn(), revoke: vi.fn(), squareSetup: vi.fn() } }))
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(apiTokens.list).mockResolvedValue([])
  vi.mocked(getEducationMode).mockResolvedValue({ enabled: false })
  vi.mocked(useLicense).mockReturnValue({ hasFeature: () => true } as any)
})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
it('requires a permission selection and submits the chosen permissions',async()=>{
  vi.mocked(apiTokens.create).mockResolvedValue({ token: 'synthetic-ui-test-value' } as any)
  render(<APITokenManager/>);fireEvent.click(await screen.findByText('New Token'))
  fireEvent.change(screen.getByLabelText('Token name'),{target:{value:'Synthetic integration'}})
  expect(screen.getByText('Create Token')).toBeDisabled()
  fireEvent.click(screen.getByText('Read Printers'));expect(screen.getByText('Create Token')).not.toBeDisabled()
  fireEvent.click(screen.getByText('Create Token'))
  await waitFor(()=>expect(apiTokens.create).toHaveBeenCalledWith({name:'Synthetic integration',scopes:['read:printers'],expires_days:null}))
  expect(await screen.findByText(/Token created. Copy it now./)).toBeInTheDocument()
})
it('identifies an invalid legacy token and preserves revocation',async()=>{
  vi.mocked(apiTokens.list).mockResolvedValue([{id:1,name:'Legacy integration',prefix:'synthetic',scopes:[]}] as any)
  vi.spyOn(window,'confirm').mockReturnValue(true)
  vi.mocked(apiTokens.revoke).mockResolvedValue({} as any)
  render(<APITokenManager/>);expect(await screen.findByText(/Invalid legacy token/)).toHaveTextContent('Revoke it')
  fireEvent.click(screen.getByRole('button',{name:'Revoke token Legacy integration'}))
  await waitFor(()=>expect(apiTokens.revoke).toHaveBeenCalledWith(1))
  await waitFor(()=>expect(screen.queryByText('Legacy integration')).not.toBeInTheDocument())
})
it('shows a failed token load',async()=>{
  vi.mocked(apiTokens.list).mockRejectedValue(new Error('Synthetic failure'))
  render(<APITokenManager/>);expect(await screen.findByText('Unable to load API tokens. Try again.')).toBeInTheDocument()
})

it('reports a failed revoke and keeps the token visible',async()=>{
  vi.mocked(apiTokens.list).mockResolvedValue([{id:2,name:'Retained integration',prefix:'synthetic',scopes:['read:jobs']}] as any)
  vi.spyOn(window,'confirm').mockReturnValue(true)
  vi.mocked(apiTokens.revoke).mockRejectedValue(new Error('Synthetic revoke failure'))
  render(<APITokenManager/>);fireEvent.click(await screen.findByRole('button',{name:'Revoke token Retained integration'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to revoke this token. Try again.')
  expect(screen.getByText('Retained integration')).toBeInTheDocument()
})

it('offers explicit production inspection and Square-send delegation',async()=>{
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'Operator', role: 'operator' })
  vi.mocked(apiTokens.create).mockResolvedValue({ token: 'synthetic-ui-production-value' } as any)
  render(<APITokenManager/>);fireEvent.click(await screen.findByText('New Token'))
  fireEvent.change(screen.getByLabelText('Token name'),{target:{value:'Production operator'}})
  fireEvent.click(screen.getByText('Record production inspections and send accepted stock to Square'))
  fireEvent.click(screen.getByText('Create Token'))
  await waitFor(()=>expect(apiTokens.create).toHaveBeenCalledWith({name:'Production operator',scopes:['write:production'],expires_days:null}))
})

function setupToken(scopes = ['read'], extra = {}) {
  vi.mocked(getCurrentUser).mockReturnValue({ id: 3, username: 'Admin', role: 'admin' })
  vi.mocked(apiTokens.list).mockResolvedValue([{ id: 7, name: 'Inventory setup', prefix: 'synthetic', scopes, ...extra }] as any)
}

it('grants and revokes Square setup on one named token and updates the list', async () => {
  setupToken()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.mocked(apiTokens.squareSetup).mockResolvedValueOnce({ id: 7, scopes: ['read', 'write:square_setup'] })
    .mockResolvedValueOnce({ id: 7, scopes: ['read'] })
  render(<APITokenManager />)
  expect(await screen.findByText(/The approval note is a record, not a verification/)).toBeInTheDocument()
  const explanation = screen.getByText(/Square setup permission is off by default/)
  expect(explanation).toHaveTextContent('map products that have no mapping and record their first stock baseline')
  expect(explanation).toHaveTextContent('Repeating the same mapping changes nothing')
  expect(explanation).toHaveTextContent('Changing a mapping or baseline needs a signed-in administrator')
  expect(explanation).toHaveTextContent("must match Square's live count")
  expect(explanation).toHaveTextContent('15 minutes old and no more than 60 seconds in the future')
  expect(screen.queryByText('write:square_setup')).not.toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: 'Grant Square setup for Inventory setup' }))
  expect(await screen.findByText('write:square_setup')).toBeInTheDocument()
  expect(apiTokens.squareSetup).toHaveBeenCalledWith(7, true)
  fireEvent.click(screen.getByRole('button', { name: 'Revoke Square setup for Inventory setup' }))
  await waitFor(() => expect(screen.queryByText('write:square_setup')).not.toBeInTheDocument())
  expect(apiTokens.squareSetup).toHaveBeenCalledWith(7, false)
})

it.each(['operator', 'viewer', 'education', 'community', 'camera'])('hides the grant for %s', async reason => {
  setupToken(reason === 'camera' ? ['read:cameras'] : ['read'])
  if (reason === 'operator' || reason === 'viewer') vi.mocked(getCurrentUser).mockReturnValue({ username: 'User', role: reason })
  if (reason === 'education') vi.mocked(getEducationMode).mockResolvedValue({ enabled: true })
  if (reason === 'community') vi.mocked(useLicense).mockReturnValue({ hasFeature: () => false } as any)
  render(<APITokenManager />)
  await screen.findByText('Inventory setup')
  expect(screen.queryByRole('button', { name: /Grant Square setup/ })).not.toBeInTheDocument()
})

it('keeps revocation available in Education mode and after license expiry', async () => {
  setupToken(['read', 'write:square_setup'])
  vi.mocked(getEducationMode).mockResolvedValue({ enabled: true })
  vi.mocked(useLicense).mockReturnValue({ hasFeature: () => false } as any)
  render(<APITokenManager />)
  expect(await screen.findByRole('button', { name: 'Revoke Square setup for Inventory setup' })).toBeEnabled()
})

it('keeps the original scopes and reports a failed grant', async () => {
  setupToken()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.mocked(apiTokens.squareSetup).mockRejectedValue(new Error('Synthetic failure'))
  render(<APITokenManager />)
  fireEvent.click(await screen.findByRole('button', { name: 'Grant Square setup for Inventory setup' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to change Square setup permission.')
  expect(screen.queryByText('write:square_setup')).not.toBeInTheDocument()
})

it('offers a viewer personal tokens within the role limits', async () => {
  vi.mocked(getCurrentUser).mockReturnValue({username: 'Student', role: 'viewer'})
  render(<APITokenManager />)
  fireEvent.click(await screen.findByRole('button', {name: 'New Token'}))
  expect(screen.getByRole('button', {name: 'Read Education'})).toBeEnabled()
  expect(screen.queryByRole('button', {name: 'Full Admin'})).not.toBeInTheDocument()
  expect(screen.queryByRole('button', {name: /Record production inspections/})).not.toBeInTheDocument()
})

it('lets the owner cancel the grant after reading what it allows', async () => {
  setupToken()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<APITokenManager />)
  fireEvent.click(await screen.findByRole('button', { name: /Grant Square setup/ }))
  expect(confirm).toHaveBeenCalledWith('Allow this token to map new products to Square and record their first stock baseline in ODIN?')
  expect(apiTokens.squareSetup).not.toHaveBeenCalled()
})

it.each([true, false])('only offers revocation on an expired token with a stored grant (%s)', async granted => {
  setupToken(granted ? ['read', 'write:square_setup'] : ['read'], { expires_at: '2000-01-01T00:00:00Z' })
  render(<APITokenManager />)
  await screen.findByText('Inventory setup')
  expect(screen.queryByRole('button', { name: /Grant Square setup/ })).toBeNull()
  expect(!!screen.queryByRole('button', { name: /Revoke Square setup/ })).toBe(granted)
})

it('shows another owner and removes their grant from the list after revocation', async () => {
  setupToken(['write:square_setup'], { owner_username: 'Alex' })
  vi.mocked(apiTokens.squareSetup).mockResolvedValue({ id: 7, scopes: ['read'] })
  render(<APITokenManager />)
  expect(await screen.findByText('Owned by Alex')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Revoke Square setup/ }))
  await waitFor(() => expect(screen.queryByText('Inventory setup')).toBeNull())
  expect(apiTokens.squareSetup).toHaveBeenCalledWith(7, false)
})

it('removes the grant button when an open account page reaches the token expiry', async () => {
  vi.useFakeTimers()
  try {
    vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
    setupToken(['read'], { expires_at: '2030-01-01T00:00:02Z' })
    await act(async () => { render(<APITokenManager />) })
    expect(screen.getByRole('button', { name: /Grant Square setup/ })).toBeEnabled()
    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.queryByRole('button', { name: /Grant Square setup/ })).toBeNull()
  } finally {
    vi.useRealTimers()
  }
})
