import { describe, expect, it } from 'vitest'
import { pricingIsConfigured } from './pricingConfigured'

describe('pricingIsConfigured', () => {
  it('is false while the settings are still loading or the request failed', () => {
    expect(pricingIsConfigured(undefined)).toBe(false)
    expect(pricingIsConfigured(null)).toBe(false)
  })
  it('is false when the server says nobody saved pricing', () => {
    expect(pricingIsConfigured({ configured: false, spool_cost: 25 })).toBe(false)
  })
  it('is true when the server says pricing was saved', () => {
    expect(pricingIsConfigured({ configured: true })).toBe(true)
  })
  it('treats an older server that does not say as configured', () => {
    expect(pricingIsConfigured({ spool_cost: 25 })).toBe(true)
  })
})
