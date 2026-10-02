import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { oidc, orgs } from '../../api'
import OIDCSettings from './OIDCSettings'

vi.mock('../../api', () => ({
  oidc: { getConfig: vi.fn(), updateConfig: vi.fn() },
  orgs: { list: vi.fn() },
}))

describe('first-time SSO setup', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    oidc.getConfig.mockResolvedValue({ configured: false })
    oidc.updateConfig.mockResolvedValue({ success: true })
    orgs.list.mockResolvedValue([])
  })

  it('explains empty tenants and refreshes without discarding unsaved settings', async () => {
    render(<OIDCSettings />)
    expect(await screen.findByText(/No organizations yet/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('OAuth client ID'), { target: { value: 'unsaved-client' } })
    orgs.list.mockResolvedValue([{ id: 3, name: 'Fixture School' }])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh tenant list' }))
    expect(await screen.findByRole('option', { name: 'Fixture School' })).toBeInTheDocument()
    expect(screen.getByLabelText('OAuth client ID')).toHaveValue('unsaved-client')
    expect(screen.queryByText(/No organizations yet/)).not.toBeInTheDocument()
  })

  it('preserves loaded configuration if the tenant list fails and supports retry', async () => {
    oidc.getConfig.mockResolvedValue({ client_id: 'saved-client' })
    orgs.list.mockRejectedValueOnce(new Error('Tenant request failed'))
    render(<OIDCSettings />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load tenants')
    expect(screen.getByLabelText('OAuth client ID')).toHaveValue('saved-client')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh tenant list' }))
    expect(await screen.findByText(/No organizations yet/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not offer saving defaults when configuration could not load', async () => {
    oidc.getConfig.mockRejectedValueOnce(new Error('Configuration request failed'))
    render(<OIDCSettings />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load SSO configuration')
    expect(screen.queryByRole('button', { name: 'Save configuration' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading configuration' }))
    expect(await screen.findByRole('button', { name: 'Save configuration' })).toBeInTheDocument()
  })

  it('does not claim success when saved configuration remains missing', async () => {
    render(<OIDCSettings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Save configuration' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not persisted')
    expect(screen.queryByText('Single sign-on configuration saved.')).not.toBeInTheDocument()
  })

  it('reloads persisted configuration before reporting success', async () => {
    render(<OIDCSettings />)
    fireEvent.change(await screen.findByLabelText('OAuth client ID'), { target: { value: 'new-client' } })
    oidc.getConfig.mockResolvedValue({ client_id: 'new-client', has_client_secret: true })
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }))
    await waitFor(() => expect(oidc.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ client_id: 'new-client' })))
    expect(await screen.findByRole('status')).toHaveTextContent('configuration saved')
    expect(screen.getByLabelText('OAuth client secret (configured)')).toHaveValue('')
  })
})
