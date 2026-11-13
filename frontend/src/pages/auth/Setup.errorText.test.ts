import { describe, expect, it } from 'vitest'
import { errorText } from './Setup'

describe('setup wizard error text', () => {
  it('turns the API error object into its sentence instead of an object React cannot render', () => {
    expect(errorText({ code: 'permission_denied', detail: 'Setup already claimed', retriable: false })).toBe('Setup already claimed')
    expect(errorText('Failed to connect.')).toBe('Failed to connect.')
    expect(errorText({ message: 'boom' })).toBe('boom')
    expect(errorText({ detail: { nested: true } })).toBe('')
    expect(errorText(undefined)).toBe('')
    expect(errorText(42)).toBe('')
  })
})
