// Pure grouping of repeated alerts. No React, no API calls.

/** Alerts of one group are chained: each is within this window of the previous member. */
export const GROUP_WINDOW_MS = 12 * 60 * 60 * 1000

export interface GroupableAlert {
  id: number
  alert_type: string
  severity: string
  title: string
  printer_id?: number | null
  is_read: boolean
  created_at: string
}

export interface AlertGroup<T extends GroupableAlert = GroupableAlert> {
  key: string
  /** Members in input order (newest first when the input is newest first). */
  alerts: T[]
  /** First member in input order: the newest alert of the group. */
  newest: T
  /** Last member in input order: the oldest alert of the group. */
  oldest: T
  /** True when any member is unread. */
  anyUnread: boolean
}

export function normaliseTitle(title: string | null | undefined): string {
  return (title ?? '').trim().toLowerCase()
}

export function groupKey(a: GroupableAlert): string {
  // JSON keeps null and numeric printer ids distinct and avoids separator collisions.
  return JSON.stringify([a.alert_type, a.severity, a.printer_id ?? null, normaliseTitle(a.title)])
}

/**
 * Groups alerts that share alert_type, severity, printer_id and normalised title,
 * chaining each one to the previous member of its group when the gap is <= windowMs.
 * Expects newest-first input (as the API returns it); order is preserved: groups
 * appear at the position of their newest member, members keep input order.
 * An alert with an unparsable timestamp never joins a group.
 * A group of one is returned as a group with a single alert.
 */
export function groupAlerts<T extends GroupableAlert>(list: T[], windowMs: number = GROUP_WINDOW_MS): AlertGroup<T>[] {
  const buckets: { key: string; alerts: T[] }[] = []
  const open = new Map<string, { key: string; alerts: T[] }>()
  for (const a of list) {
    const key = groupKey(a)
    const bucket = open.get(key)
    if (bucket) {
      const last = bucket.alerts[bucket.alerts.length - 1]
      const gap = Math.abs(Date.parse(last.created_at) - Date.parse(a.created_at))
      if (gap <= windowMs) { // false for NaN, so bad timestamps start a new group
        bucket.alerts.push(a)
        continue
      }
    }
    const fresh = { key, alerts: [a] }
    buckets.push(fresh)
    open.set(key, fresh)
  }
  return buckets.map(b => ({
    key: b.key,
    alerts: b.alerts,
    newest: b.alerts[0],
    oldest: b.alerts[b.alerts.length - 1],
    anyUnread: b.alerts.some(a => !a.is_read),
  }))
}
