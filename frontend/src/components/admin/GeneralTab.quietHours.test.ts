import { describe, expect, it } from 'vitest'
import { quietHoursSummary, timeZoneChoices } from './GeneralTab'

describe('quiet hours summary', () => {
  it('says off when start equals end, with or without a time zone', () => {
    expect(quietHoursSummary('00:00', '00:00')).toMatch(/^Quiet hours are off/)
    expect(quietHoursSummary('07:15', '07:15')).toMatch(/^Quiet hours are off/)
    expect(quietHoursSummary('00:00', '00:00', 'America/New_York')).toMatch(/^Quiet hours are off/)
  })
  it('a window with no saved time zone names UTC and gives the viewer the local clock time', () => {
    const text = quietHoursSummary('22:30', '05:30', null, 'America/New_York')
    expect(text).toContain('between 22:30 and 05:30 UTC')
    expect(text).toMatch(/5:30\s?PM to 12:30\s?AM your time/)
    expect(quietHoursSummary('22:30', '05:30', '', 'America/New_York')).toBe(text)
  })
  it('a window with a time zone is in that zone all year and never mentions UTC', () => {
    const text = quietHoursSummary('22:30', '05:30', 'America/New_York')
    expect(text).toBe('The scheduler will not start jobs between 22:30 and 05:30, America/New_York time, all year (daylight-saving changes are followed). Save to apply.')
    expect(text).not.toContain('UTC')
  })
  it('the zone list always holds UTC and the zones it is asked to keep, once each', () => {
    const zones = timeZoneChoices('America/New_York', 'Test/Unlisted', '', 'UTC')
    expect(zones).toEqual(expect.arrayContaining(['UTC', 'America/New_York', 'Test/Unlisted']))
    expect(zones.filter(z => z === 'UTC')).toHaveLength(1)
    expect(zones).not.toContain('')
  })
})
