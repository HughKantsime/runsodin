import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import ProtectedRoute from '../../components/auth/ProtectedRoute'
import { getCurrentUser } from '../../permissions'
import Account from './Account'

vi.mock('../../components/auth/MFASetup', () => ({default: () => null}))
vi.mock('../../components/auth/APITokenManager', () => ({default: () => null}))
vi.mock('../../components/auth/SessionManager', () => ({default: () => null}))

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function openAccount(hasLocalPassword = true, unauthorizedDetail?: string | {code: string; message: string}) {
  const request = vi.fn(async (url: string, _options?: RequestInit) => {
    if (url === '/api/auth/me') return Response.json({id: 1, username: 'reader', role: 'viewer', has_local_password: hasLocalPassword})
    if (url === '/api/permissions') return Response.json({})
    if (url === '/api/auth/me/password') return unauthorizedDetail
      ? Response.json({detail: unauthorizedDetail}, {status: 401}) : Response.json({status: 'updated'})
    throw new Error('Unexpected request')
  })
  vi.stubGlobal('fetch', request)
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/account']}>
    <ProtectedRoute><Account /></ProtectedRoute>
  </MemoryRouter></QueryClientProvider>)
  await screen.findByRole('heading', {name: 'Password'})
  return request
}

function submit(confirm = 'Replacement1') {
  fireEvent.change(screen.getByLabelText('Current password'), {target: {value: 'Current1'}})
  fireEvent.change(screen.getByLabelText('New password'), {target: {value: 'Replacement1'}})
  fireEvent.change(screen.getByLabelText('Confirm new password'), {target: {value: confirm}})
  fireEvent.click(screen.getByRole('button', {name: 'Change password'}))
}

test('changes the password, clears the fields and returns focus to the card', async () => {
  const request = await openAccount()
  submit()
  expect(await screen.findByRole('status')).toHaveTextContent('Your other devices were signed out.')
  expect(request).toHaveBeenCalledWith('/api/auth/me/password', expect.objectContaining({
    method: 'POST', credentials: 'include', body: JSON.stringify({current_password: 'Current1', new_password: 'Replacement1'}),
  }))
  for (const label of ['Current password', 'New password', 'Confirm new password']) {
    expect(screen.getByLabelText(label)).toHaveValue('')
  }
  expect(screen.getByRole('heading', {name: 'Password'})).toHaveFocus()
})

test('shows the wrong-password response and keeps the account page open', async () => {
  vi.stubGlobal('location', {pathname: '/account', href: '/account'})
  await openAccount(true, {code: 'wrong_current_password', message: 'Check your current password and try again.'})
  submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('Check your current password')
  expect(window.location.href).toBe('/account')
  expect(screen.getByRole('heading', {name: 'Password'})).toHaveFocus()
})

test.each([
  'Not authenticated', 'Sign in again to change your password.', 'Check your current password and try again.',
])('redirects to sign in for an unmarked 401 (%s)', async detail => {
  vi.stubGlobal('location', {pathname: '/account', href: '/account'})
  await openAccount(true, detail)
  submit()
  await waitFor(() => expect(window.location.href).toBe('/login'))
  expect(getCurrentUser()).toBeNull()
  for (const label of ['Current password', 'New password', 'Confirm new password']) {
    expect(screen.getByLabelText(label)).toHaveValue('')
  }
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('status')).toBeNull()
})

test('requires matching new passwords before sending the request', async () => {
  const request = await openAccount()
  submit('SomethingElse1')
  expect(screen.getByRole('alert')).toHaveTextContent('The new passwords do not match.')
  expect(request.mock.calls.some(([url]) => url.endsWith('/password'))).toBe(false)
})

test('single sign-on accounts see where to manage their password', async () => {
  await openAccount(false)
  expect(screen.getByText('Manage your password through your single sign-on provider.')).toBeVisible()
  expect(screen.queryByLabelText('Current password')).toBeNull()
  expect(screen.queryByRole('button', {name: 'Change password'})).toBeNull()
})
