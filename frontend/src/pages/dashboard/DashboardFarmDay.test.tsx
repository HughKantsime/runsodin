// "Completed today" is the farm's day. The tile names the farm zone only when this viewer's day
// starts at a different moment.
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'
import { I18nProvider } from '../../contexts/I18nContext'
import { farmDayNote, zoneOffsetMinutes, zoneShortName } from '../../utils/farmTime'

vi.mock('../../api', () => ({ fetchAPI: vi.fn().mockResolvedValue({}), stats: { get: vi.fn() }, jobs: { list: vi.fn() }, printers: { list: vi.fn() }, printJobs: { list: vi.fn() }, alerts: { summary: vi.fn() }, maintenance: { getStatus: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
const realResolved = Intl.DateTimeFormat.prototype.resolvedOptions
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear() })

function show(config: unknown, viewerZone: string) {
  vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockImplementation(function (this: Intl.DateTimeFormat) {
    return { ...realResolved.call(this), timeZone: viewerZone }
  })
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['stats'], { jobs: { completed_today: 4 } }); client.setQueryData(['jobs', 'active'], []); client.setQueryData(['print-jobs'], [])
  client.setQueryData(['printers'], []); client.setQueryData(['maintenance-status'], [])
  if (config) client.setQueryData(['config'], config)
  return render(<QueryClientProvider client={client}><MemoryRouter><I18nProvider><Dashboard /></I18nProvider></MemoryRouter></QueryClientProvider>)
}
const farm = (zone: string, source = 'setting') => ({ farm_timezone: source === 'setting' ? zone : null, farm_timezone_effective: { zone, source } })
const JANUARY = new Date('2026-01-15T17:00:00Z')

describe('dashboard "Completed today" label', () => {
  it('names the farm zone for a viewer in another zone', () => {
    vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(JANUARY)
    show(farm('America/New_York'), 'Europe/Berlin')
    expect(screen.getByText('Completed Today (farm time, EST)')).toBeInTheDocument()
  })
  it('is unchanged for a viewer in the farm zone', () => {
    show(farm('America/New_York'), 'America/New_York')
    expect(screen.getByText('Completed Today')).toBeInTheDocument()
    expect(screen.queryByText(/farm time/)).toBeNull()
  })
  it('is unchanged for a viewer in a different zone on the same clock', () => {
    show(farm('America/New_York'), 'America/Toronto')
    expect(screen.getByText('Completed Today')).toBeInTheDocument()
  })
  it('is labelled when the farm follows UTC and the viewer does not', () => {
    show(farm('UTC', 'utc'), 'America/Chicago')
    expect(screen.getByText('Completed Today (farm time, UTC)')).toBeInTheDocument()
  })
  it('is unchanged before the farm zone is known', () => {
    show(null, 'Europe/Berlin')
    expect(screen.getByText('Completed Today')).toBeInTheDocument()
  })
})

describe('farmDayNote', () => {
  it('follows daylight saving in the short name', () => {
    expect(farmDayNote('America/New_York', 'UTC', JANUARY)).toBe('farm time, EST')
    expect(farmDayNote('America/New_York', 'UTC', new Date('2026-07-15T16:00:00Z'))).toBe('farm time, EDT')
    expect(zoneShortName('America/New_York', JANUARY)).toBe('EST')
  })
  it('is empty when the zones are the same, on the same clock, missing or unknown', () => {
    expect(farmDayNote('America/New_York', 'America/New_York', JANUARY)).toBe('')
    expect(farmDayNote('America/New_York', 'America/Toronto', JANUARY)).toBe('')
    expect(farmDayNote('UTC', 'Etc/UTC', JANUARY)).toBe('')
    expect(farmDayNote(null, 'Europe/Berlin', JANUARY)).toBe('')
    expect(farmDayNote(undefined, 'Europe/Berlin', JANUARY)).toBe('')
    expect(farmDayNote('Mars/Olympus', 'Europe/Berlin', JANUARY)).toBe('')
  })
  it('compares the clocks at this moment: London and UTC differ only in summer', () => {
    expect(farmDayNote('Europe/London', 'UTC', JANUARY)).toBe('')
    expect(farmDayNote('Europe/London', 'UTC', new Date('2026-07-15T16:00:00Z'))).not.toBe('')
    expect(zoneOffsetMinutes('America/New_York', JANUARY)).toBe(-300)
    expect(zoneOffsetMinutes('Asia/Kolkata', JANUARY)).toBe(330)
    expect(zoneOffsetMinutes('Mars/Olympus', JANUARY)).toBeNull()
  })
})
