// The farm's time zone (Settings > General): the server counts "today" and daily totals in it.
// Instants are still shown in each viewer's own zone; these helpers only say when the two differ.

/** Minutes east of UTC that `zone` is at `at`; null for a zone this browser does not know. */
export function zoneOffsetMinutes(zone: string, at: Date = new Date()): number | null {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric' }).formatToParts(at)
    const n = (type: string) => Number(parts.find(p => p.type === type)?.value)
    return Math.round((Date.UTC(n('year'), n('month') - 1, n('day'), n('hour'), n('minute')) - at.getTime()) / 60000)
  } catch { return null }
}

/** Short name of `zone` at `at`, such as "EST"; the IANA name when the browser has none. */
export function zoneShortName(zone: string, at: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'short' }).formatToParts(at).find(p => p.type === 'timeZoneName')?.value || zone
  } catch { return zone }
}

/** "farm time, EST" when the farm's day starts at a different moment from the viewer's; '' when
 *  the two zones are on the same clock right now, or the farm zone is not known yet. */
export function farmDayNote(farmZone: string | null | undefined, viewerZone: string, at: Date = new Date()): string {
  if (!farmZone || farmZone === viewerZone) return ''
  const farm = zoneOffsetMinutes(farmZone, at)
  if (farm === null || farm === zoneOffsetMinutes(viewerZone, at)) return ''
  return `farm time, ${zoneShortName(farmZone, at)}`
}

/** The IANA time zone this browser is set to. */
export function browserTimeZone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' }
}

/** Time zones offered in Settings: what the browser knows, plus any the caller needs listed. */
export function timeZoneChoices(...always: string[]): string[] {
  let known: string[] = []
  try { known = (Intl as any).supportedValuesOf?.('timeZone') ?? [] } catch { known = [] }
  return Array.from(new Set([...always.filter(Boolean), 'UTC', ...known])).sort()
}
