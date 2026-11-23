export type HmsEvent = { code?: string; message?: string; severity?: string; wiki_url?: string; raw_code?: number; informational_only?: boolean }
export function hmsEvents(value: unknown): HmsEvent[] {
  if (typeof value === 'string') { try { value = JSON.parse(value) } catch { return [] } }
  return Array.isArray(value) ? value.filter(e => e?.display !== false) : []
}
export function hmsFaults(value: unknown): HmsEvent[] {
  return hmsEvents(value).filter(e => e?.informational_only !== true && e?.severity !== 'info')
}
