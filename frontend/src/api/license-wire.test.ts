import { afterEach, expect, test, vi } from 'vitest'
import { license } from './auth'
import fixture from '../test-fixtures/public-license-wire-owned.json'

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })
test('typed public license client reads the actual wire through the credentialed endpoint', async () => {
  const transport = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture.body), { status:200, headers:{'Content-Type':'application/json'} }))
  vi.stubGlobal('fetch', transport)
  expect(await license.get()).toEqual(fixture.body)
  expect(transport).toHaveBeenCalledExactlyOnceWith('/api/license', expect.objectContaining({ credentials:'include' }))
})
