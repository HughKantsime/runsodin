import React, { StrictMode, useEffect } from 'react'
import { cleanup, render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import OIDCBootstrap, { oidcNavigation } from './OIDCBootstrap'

const request = vi.fn()
const mounted = vi.fn()
function Providers() { useEffect(mounted, []); return <div>Application</div> }
const response = (body: unknown = { username: 'teacher', role: 'operator' }, ok = true) =>
  ({ ok, redirected: false, json: async () => body })
beforeEach(() => {
  vi.stubGlobal('fetch', request)
  vi.spyOn(oidcNavigation, 'finish').mockImplementation(() => {})
  history.replaceState(null, '', '/')
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); request.mockReset(); mounted.mockReset() })
function start(path: string) {
  history.replaceState(null, '', path)
  return render(<StrictMode><OIDCBootstrap><Providers /></OIDCBootstrap></StrictMode>)
}

it.each(['/', '/login'])('exchanges once before providers at %s, even with delayed StrictMode replay', async path => {
  let finish!: (value: unknown) => void
  request.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    .mockResolvedValueOnce(response())
  start(`${path}?oidc_code=synthetic-code`)
  expect(screen.getByRole('status')).toBeInTheDocument()
  expect(location.search).toBe('')
  expect(request).toHaveBeenCalledTimes(1)
  expect(mounted).not.toHaveBeenCalled()
  await act(async () => finish(response()))
  await waitFor(() => expect(oidcNavigation.finish).toHaveBeenCalledTimes(1))
  expect(request).toHaveBeenNthCalledWith(1, '/api/auth/oidc/exchange', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ code: 'synthetic-code' }), credentials: 'include', cache: 'no-store', redirect: 'error',
  }))
  expect(request).toHaveBeenNthCalledWith(2, '/api/auth/me', expect.objectContaining({ credentials: 'include', cache: 'no-store', redirect: 'error' }))
  expect(mounted).not.toHaveBeenCalled()
})

it.each(['/?oidc_code=', '/?error=secret', '/login?error=', '/?oidc_code=a&error=x', '/?oidc_code=a&oidc_code=b'])('rejects malformed/provider callback %s', async path => {
  start(path)
  expect(await screen.findByRole('alert')).toHaveTextContent('SSO sign-in could not be completed')
  expect(screen.getByRole('link')).toHaveAttribute('href', '/login')
  expect(location.search).toBe('')
  expect(request).not.toHaveBeenCalled()
  expect(mounted).not.toHaveBeenCalled()
  expect(oidcNavigation.finish).not.toHaveBeenCalled()
})

it.each(['exchange', 'network', 'cookie', 'shape', 'json', 'redirect'])('fails closed on %s failure', async failure => {
  if (failure === 'network') request.mockRejectedValue(new Error('sensitive backend details'))
  else if (failure === 'exchange') request.mockResolvedValue(response({}, false))
  else request.mockResolvedValueOnce(response()).mockResolvedValueOnce(
    failure === 'cookie' ? response({}, false) : failure === 'redirect' ? { ...response(), redirected: true } :
      failure === 'json' ? { ...response(), json: async () => { throw new SyntaxError('Invalid JSON') } } : response({})
  )
  start('/?oidc_code=synthetic-code')
  expect(await screen.findByRole('alert')).not.toHaveTextContent('sensitive')
  expect(request).toHaveBeenCalledTimes(['exchange', 'network'].includes(failure) ? 1 : 2)
  expect(mounted).not.toHaveBeenCalled()
  expect(oidcNavigation.finish).not.toHaveBeenCalled()
})

it('waits for delayed session verification without mounting providers or exchanging again', async () => {
  let finish!: (value: unknown) => void
  request.mockResolvedValueOnce(response()).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  start('/?oidc_code=synthetic-code')
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
  expect(mounted).not.toHaveBeenCalled()
  expect(oidcNavigation.finish).not.toHaveBeenCalled()
  expect(screen.getByRole('status')).toBeInTheDocument()
  await act(async () => finish(response()))
  await waitFor(() => expect(oidcNavigation.finish).toHaveBeenCalledTimes(1))
  expect(request).toHaveBeenCalledTimes(2)
  expect(mounted).not.toHaveBeenCalled()
})

it.each(['/', '/login', '/printers', '/printers?error=filter'])('leaves normal route %s alone', path => {
  start(path)
  expect(screen.getByText('Application')).toBeInTheDocument()
  expect(request).not.toHaveBeenCalled()
  expect(location.pathname + location.search).toBe(path)
})
