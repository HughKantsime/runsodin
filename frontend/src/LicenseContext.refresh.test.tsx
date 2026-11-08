import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { LicenseProvider, useLicense, licenseExpiryMillis } from './LicenseContext'
import { fetchAPI, downloadBlob } from './api/client'
import { LICENSE_REFRESH_EVENT } from './api/licenseEvents'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('./api', () => ({ license: { get: api.get } }))
const start = Date.parse('2026-11-08T00:00:00Z')
const active = { valid: true, tier: 'pro', features: ['csv_export'], expires_at: '2026-11-08T00:01:00Z' }
const expired = { valid: false, tier: 'pro', features: ['cost_calculator'] }
function Probe() {
  const license = useLicense()
  return <span>{license.hasFeature('csv_export') ? 'CSV enabled' : 'CSV disabled'}; {license.tier}; {license.loadError ? 'refresh failed' : 'loaded'}</span>
}
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(start); api.get.mockReset() })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
async function mount() {
  await act(async () => { render(<LicenseProvider><Probe /></LicenseProvider>) })
}

test('refreshes at expiry with no navigation or file mutation', async () => {
  api.get.mockResolvedValueOnce(active).mockResolvedValue(expired)
  await mount()
  expect(screen.getByText(/CSV enabled/)).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(60001) })
  expect(api.get).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/CSV disabled/)).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(600000) })
  expect(api.get).toHaveBeenCalledTimes(2)
})

test('focus and concurrent feature refusals refresh and coalesce', async () => {
  api.get.mockResolvedValueOnce(active).mockResolvedValue(expired)
  await mount()
  await act(async () => {
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event(LICENSE_REFRESH_EVENT))
    window.dispatchEvent(new Event(LICENSE_REFRESH_EVENT))
  })
  expect(api.get).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/CSV disabled/)).toBeInTheDocument()
})

test.each(['json', 'blob'])('feature 403 from %s transport refreshes license and preserves error', async format => {
  api.get.mockResolvedValueOnce(active).mockResolvedValue(expired)
  await mount()
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: "Feature 'csv_export' requires Pro" }), {
    status: 403, headers: { 'Content-Type': 'application/json', 'X-ODIN-License-Feature': 'csv_export' },
  }))
  await act(async () => {
    await expect(format === 'json' ? fetchAPI('/export/jobs') : downloadBlob('/export/jobs', 'jobs.csv')).rejects.toThrow('requires Pro')
  })
  expect(api.get).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/CSV disabled/)).toBeInTheDocument()
})

test('ordinary RBAC 403 does not refresh license', async () => {
  api.get.mockResolvedValue(active)
  await mount()
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: 'Admin required' }), { status: 403 }))
  await expect(fetchAPI('/users')).rejects.toThrow('Admin required')
  expect(api.get).toHaveBeenCalledTimes(1)
})

test('date-only and naive expiry match UTC backend semantics; long timers do not overflow', async () => {
  expect(licenseExpiryMillis('2026-11-08')).toBe(start + 86400000)
  expect(licenseExpiryMillis('2026-11-08T00:01:00')).toBe(start + 60000)
  expect(licenseExpiryMillis('2026-11-08T02:01:00+02:00')).toBe(start + 60000)
  api.get.mockResolvedValue({ ...active, expires_at: '2099-01-01' })
  await mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(2147483647) })
  expect(api.get).toHaveBeenCalledTimes(1)
})

test.each(['focus', LICENSE_REFRESH_EVENT])('failed %s refresh preserves unexpired features; unmount cleans up', async event => {
  api.get.mockResolvedValueOnce(active).mockRejectedValue(new Error('offline'))
  await mount()
  await act(async () => { window.dispatchEvent(new Event(event)) })
  expect(screen.getByText(/CSV enabled; pro; refresh failed/)).toBeInTheDocument()
  cleanup()
  window.dispatchEvent(new Event('focus'))
  window.dispatchEvent(new Event(LICENSE_REFRESH_EVENT))
  await vi.advanceTimersByTimeAsync(120000)
  expect(api.get).toHaveBeenCalledTimes(2)
})


test.each(['focus', LICENSE_REFRESH_EVENT, 'expiry'])('failed %s refresh drops expired features to Community', async event => {
  api.get.mockResolvedValueOnce(active).mockRejectedValue(new Error('Service unavailable'))
  await mount()
  await act(async () => {
    if (event === 'expiry') await vi.advanceTimersByTimeAsync(60001)
    else {
      vi.setSystemTime(start + 60000)
      window.dispatchEvent(new Event(event))
    }
  })
  expect(api.get).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/CSV disabled; community; refresh failed/)).toBeInTheDocument()
})

test('initial load failure remains Community and a later focus can recover', async () => {
  api.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(active)
  await mount()
  expect(screen.getByText(/CSV disabled; community; refresh failed/)).toBeInTheDocument()
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  expect(screen.getByText(/CSV enabled; pro; loaded/)).toBeInTheDocument()
})
