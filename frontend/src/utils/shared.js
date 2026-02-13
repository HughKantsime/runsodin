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

export function formatDuration(minutes) {
  if (!minutes || minutes <= 0) return '0m'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  if (h === 0) return `${m}m`
  if (m === 0) return `${h}h`
  return `${h}h ${m}m`
}
