import { describe, expect, it } from 'vitest'
import { displayName } from './displayName'

describe('human print names', () => {
  it('uses the server model name while retaining stored transport identity', () => {
    const row = { display_name: 'Squirtle', item_name: 'odin-dispatch-ab12-34' }
    expect(displayName(row)).toBe('Squirtle')
    expect(row.item_name).toBe('odin-dispatch-ab12-34')
  })
  it('suppresses transport names on older and unlinked payloads', () => {
    for (const key of ['item_name', 'job_name', 'print_name', 'filename']) {
      expect(displayName({ [key]: 'Reprint: odin-dispatch-deadbeef.gcode' })).toBe('Unnamed print')
    }
  })
  it('keeps useful fallback names', () => {
    expect(displayName({ job_name: 'Bracket' })).toBe('Bracket')
  })
})
