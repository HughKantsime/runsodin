import React from 'react'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import APITokenManager from './APITokenManager'
import { apiTokens } from '../../api'
vi.mock('../../api', () => ({ apiTokens: { list: vi.fn(), create: vi.fn(), revoke: vi.fn() } }))
beforeEach(() => { vi.resetAllMocks();vi.mocked(apiTokens.list).mockResolvedValue([]) })
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
  vi.mocked(apiTokens.create).mockResolvedValue({ token: 'synthetic-ui-production-value' } as any)
  render(<APITokenManager/>);fireEvent.click(await screen.findByText('New Token'))
  fireEvent.change(screen.getByLabelText('Token name'),{target:{value:'Production operator'}})
  fireEvent.click(screen.getByText('Record production inspections and send accepted stock to Square'))
  fireEvent.click(screen.getByText('Create Token'))
  await waitFor(()=>expect(apiTokens.create).toHaveBeenCalledWith({name:'Production operator',scopes:['write:production'],expires_days:null}))
})
