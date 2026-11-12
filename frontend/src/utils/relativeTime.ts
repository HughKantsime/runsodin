/**
 * "N minutes/hours/days ago" from an age in minutes.
 * Minutes under 1 h, hours under 48 h, then days. Under 1 minute (or not a number) reads "just now".
 * (AlertBell and DetectionFeed keep private helpers with different cut-offs; they are left alone.)
 */
export function formatMinutesAgo(minutes: number): string {
  const m = Math.floor(minutes)
  if (!Number.isFinite(m) || m < 1) return 'just now'
  if (m < 60) return `${m} ${m === 1 ? 'minute' : 'minutes'} ago`
  const hours = Math.floor(m / 60)
  if (hours < 48) return `${hours} ${hours === 1 ? 'hour' : 'hours'} ago`
  return `${Math.floor(hours / 24)} days ago`
}
