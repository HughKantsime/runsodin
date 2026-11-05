export type HmsEvent = { code?: string; message?: string; severity?: string; wiki_url?: string; raw_code?: number }
export function hmsEvents(value: unknown): HmsEvent[] {
  if (typeof value === 'string') { try { value = JSON.parse(value) } catch { return [] } }
  return Array.isArray(value) ? value : []
}
export function hmsFaults(value: unknown): HmsEvent[] {
  return hmsEvents(value).filter(e => {
    // Old/raw packets may still arrive during an upgrade.
    const raw = e?.raw_code ?? (typeof e?.code === 'number' ? e.code : undefined)
    if (typeof raw === 'number') return (raw >>> 16) === 1 || (raw >>> 16) === 2
    return !e?.severity || ['critical', 'error', 'warning'].includes(e.severity)
  })
}
