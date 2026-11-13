import { describe, expect, it } from 'vitest'
import { quietHoursSummary } from './GeneralTab'

describe('quiet hours summary', () => {
  it('says off when start equals end', () => {
    expect(quietHoursSummary('00:00', '00:00')).toMatch(/^Quiet hours are off/)
    expect(quietHoursSummary('07:15', '07:15')).toMatch(/^Quiet hours are off/)
  })
  it('names UTC and gives the viewer the local clock time', () => {
    const text = quietHoursSummary('22:30', '05:30', 'America/New_York')
    expect(text).toContain('between 22:30 and 05:30 UTC')
    expect(text).toMatch(/5:30\s?PM to 12:30\s?AM your time/)
  })
})
