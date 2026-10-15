// Cert B: the OBS overlay camera uses the token-guarded MJPEG relay (F02-2.8-02).
// /api/overlay/{id} response is the producer wire fixture written by
// tests/test_contracts/test_cert_B_camera_fixes.py::test_overlay_points_at_token_guarded_mjpeg_relay.
import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import wire from '../../test-fixtures/cert-B-wire.json'
import Overlay from './Overlay'

const requests: string[] = []
beforeEach(() => {
  requests.length = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string) => { requests.push(url); return new Response(JSON.stringify(wire.overlay), { status: 200 }) }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount(entry: string) {
  return render(<MemoryRouter initialEntries={[entry]}><Routes><Route path="/overlay/:printerId" element={<Overlay />} /></Routes></MemoryRouter>)
}

test('copied overlay URL token is passed to the camera relay', async () => {
  mount('/overlay/9?token=odin_synthetic%2Btoken')
  const img = await screen.findByAltText('Camera stream')
  expect(img.getAttribute('src')).toBe('/api/overlay/9/camera?token=odin_synthetic%2Btoken')
  expect(requests).toEqual(['/api/overlay/9'])  // status polling never carries the token
})

test('without a token the overlay is status-only (no broken camera panel)', async () => {
  mount('/overlay/9')
  expect(await screen.findByTestId('overlay-printer-name')).toHaveTextContent('P1S-09')
  expect(screen.queryByAltText('Camera stream')).toBeNull()
})

test('camera=false hides the camera even with a token', async () => {
  mount('/overlay/9?token=odin_synthetic&camera=false')
  expect(await screen.findByTestId('overlay-printer-name')).toBeInTheDocument()
  expect(screen.queryByAltText('Camera stream')).toBeNull()
})
