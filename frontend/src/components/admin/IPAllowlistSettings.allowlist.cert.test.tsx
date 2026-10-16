/**
 * Cert F / cluster C - F12-12.2-02/03: IPAllowlistSettings wired to the real allowlist API contract.
 *
 * Real IPAllowlistSettings + real api client (ipAllowlist.get/set). window.fetch replays producer wire captured
 * from a real ODIN server, including the lock-out check (a list that excludes the caller is refused with the address the server sees)
 * and the real 400 for an invalid CIDR. test_cert_F_C_ui_wire.py checks the embedded JSON against the capture.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import IPAllowlistSettings from './IPAllowlistSettings'

const WIRE = /*WIRE-BEGIN*/{"allowlist_default": {"cidrs": [], "enabled": false, "mode": "api_and_ui"}, "allowlist_put": {"body": {"cidrs": ["10.20.0.0/16"], "enabled": true, "mode": "api_and_ui"}, "status": 200}, "allowlist_put_invalid": {"body": {"detail": "Invalid CIDR: 10.20.0.0/33", "error": {"code": "http_error", "detail": "Invalid CIDR: 10.20.0.0/33", "retriable": false}}, "status": 400}, "allowlist_put_lockout": {"body": {"detail": "This server sees your address as 198.51.100.23, which the allowlist does not include: saving it would lock you out. Add 198.51.100.23 or its network. If 198.51.100.23 is a reverse proxy, every client behind it shares that address.", "error": {"code": "http_error", "detail": "This server sees your address as 198.51.100.23, which the allowlist does not include: saving it would lock you out. Add 198.51.100.23 or its network. If 198.51.100.23 is a reverse proxy, every client behind it shares that address.", "retriable": false}}, "status": 400}}/*WIRE-END*/

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function serve(put: { status: number, body: unknown }) {
  const calls: Array<{ method: string, body: any }> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (String(input) !== '/api/config/ip-allowlist') return json(404, { detail: 'Not Found' })
    return method === 'PUT' ? json(put.status, put.body) : json(200, WIRE.allowlist_default)
  }))
  return calls
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('F12-12.2-03 enabling and adding a CIDR sends the real PUT and shows the server lock-out exception', async () => {
  const calls = serve(WIRE.allowlist_put)
  render(<IPAllowlistSettings />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Enable IP allowlisting' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'IP address or CIDR' }), { target: { value: '10.20.0.0/16' } })
  fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  expect(screen.getByText('10.20.0.0/16')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Save/ }))
  await screen.findByText('IP allowlist saved')
  expect(calls.find(c => c.method === 'PUT')!.body).toEqual({ enabled: true, cidrs: ['10.20.0.0/16'], mode: 'api_and_ui' })
  // The server adds nothing on the caller's behalf (security review LOW 7).
  expect(screen.queryByText('127.0.0.1/32')).toBeNull()
})

it('F12-12.2-02 a list that excludes the caller is refused and the address the server sees is shown', async () => {
  serve(WIRE.allowlist_put_lockout)
  render(<IPAllowlistSettings />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Enable IP allowlisting' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'IP address or CIDR' }), { target: { value: '10.20.0.0/16' } })
  fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  fireEvent.click(screen.getByRole('button', { name: /Save/ }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This server sees your address as 198.51.100.23')
  expect(screen.queryByText('IP allowlist saved')).toBeNull()
})

it('F12-12.2-03 bare addresses become host CIDRs and malformed input is refused locally', async () => {
  const calls = serve(WIRE.allowlist_put)
  render(<IPAllowlistSettings />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Enable IP allowlisting' }))
  const box = screen.getByRole('textbox', { name: 'IP address or CIDR' })
  fireEvent.change(box, { target: { value: '10.0.0.5' } }); fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  fireEvent.change(box, { target: { value: '2001:db8::7' } }); fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  expect(screen.getByText('10.0.0.5/32')).toBeInTheDocument()
  expect(screen.getByText('2001:db8::7/128')).toBeInTheDocument()
  fireEvent.change(box, { target: { value: 'not an ip' } }); fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  expect(screen.getByRole('alert')).toHaveTextContent('Invalid IP or CIDR format')
  expect(calls.filter(c => c.method === 'PUT')).toHaveLength(0)
})

it('F12-12.2-03 server-side CIDR refusal is shown and nothing is reported saved', async () => {
  serve(WIRE.allowlist_put_invalid)
  render(<IPAllowlistSettings />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Enable IP allowlisting' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'IP address or CIDR' }), { target: { value: '10.20.0.0/33' } })
  fireEvent.click(screen.getByRole('button', { name: /Add/ }))
  fireEvent.click(screen.getByRole('button', { name: /Save/ }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Invalid CIDR: 10.20.0.0/33'))
  expect(screen.queryByText('IP allowlist saved')).not.toBeInTheDocument()
})
