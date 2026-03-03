export const ONLINE_THRESHOLD_MS = 90_000

export function getShortName(slot) {
  const color = typeof slot === 'string' ? slot : slot?.color
  const ft = typeof slot === 'string' ? '' : (slot?.filament_type || '')
  const fallback = (!ft || ft === 'empty') ? 'Empty' : ft
  if (!color || color.startsWith('#') || /^[0-9a-fA-F]{6}$/.test(color)) return fallback
  const brands = ['Bambu Lab', 'Polymaker', 'Hatchbox', 'eSun', 'Prusament', 'Overture', 'Generic']
  let short = color
  for (const brand of brands) {
    if (color.startsWith(brand + ' ')) {
      short = color.slice(brand.length + 1)
      break
    }
  }
  if (short.length > 12) return short.slice(0, 10) + '...'
  return short
}

/** Format a duration given in minutes to "Xh Ym" display. */
export function formatDuration(minutes) {
  if (!minutes || minutes <= 0) return '0m'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  if (h === 0) return `${m}m`
  if (m === 0) return `${h}h`
  return `${h}h ${m}m`
}

/** Format a duration given in seconds to "Xh Ym" or "Xm Ys" display. */
export function formatDurationSecs(seconds) {
  if (!seconds) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** Format an ISO date string to locale-friendly display (e.g. "Jan 5, 2026, 03:45 PM"). */
export function formatDate(d) {
  if (!d) return '--'
  return new Date(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** Format an ISO datetime string to time-only display (e.g. "3:45 PM"). */
export function formatTime(isoString) {
  if (!isoString) return '\u2014'
  const d = new Date(isoString)
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

/** Format decimal hours to "Xh Ym" display. */
export function formatHours(hours) {
  if (!hours) return '\u2014'
  const mins = Math.round(hours * 60)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  const m = mins % 60
  return m > 0 ? `${hrs}h ${m}m` : `${hrs}h`
}

/** Format a file size given in megabytes to human-readable display. */
export function formatSize(mb) {
  if (!mb) return '-'
  if (mb < 1) return `${Math.round(mb * 1024)} KB`
  return `${mb.toFixed(1)} MB`
}

/** Format seconds to MM:SS display (zero-padded). */
export function formatMMSS(sec) {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** Check whether a printer is online based on its last_seen timestamp. */
export function isOnline(printer) {
  return printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < ONLINE_THRESHOLD_MS
}
