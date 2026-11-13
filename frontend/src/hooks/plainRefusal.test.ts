import { describe, expect, it } from 'vitest'
import { plainRefusal } from './useJobMutations'

describe('plainRefusal', () => {
  it('drops the code and says where to fix a missing filament mapping', () => {
    expect(plainRefusal('ams_mapping_required: No AMS mapping: choose the tray for each filament. no upload or start was sent.'))
      .toBe('No AMS mapping: choose the tray for each filament. no upload or start was sent. To fix it: open the job with the pencil button and choose a filament slot for each colour.')
  })
  it('drops an unknown code and keeps the sentence', () => {
    expect(plainRefusal('s1_start_rejected: Printer refused the start')).toBe('Printer refused the start')
  })
  it('leaves an ordinary sentence alone, including one with a colon', () => {
    expect(plainRefusal('Printer is offline: check power')).toBe('Printer is offline: check power')
    expect(plainRefusal('')).toBe('')
    expect(plainRefusal(undefined)).toBe('')
  })
})
