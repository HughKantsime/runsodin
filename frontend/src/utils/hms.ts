export type HmsEvent = { attr?: number; code?: string | number; message?: string; severity?: string; wiki_url?: string; raw_code?: number; informational_only?: boolean }

function isCodeWord(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 0xffffffff
}

export function isHmsNotice(event: HmsEvent) {
  if (event?.informational_only === true || event?.severity === 'info') return true
  if (['critical', 'error', 'warning'].includes(event?.severity ?? '')) return false
  const code = event?.code
  const raw = isCodeWord(event?.raw_code) ? event.raw_code : code
  let level
  if (isCodeWord(raw)) level = raw >>> 16
  else if (typeof code === 'string' && /^[0-9a-f]{8}_[0-9a-f]{8}$/i.test(code)) level = parseInt(code.slice(9, 13), 16)
  return level === 3 || level === 4
}

export function hmsEvents(value: unknown): HmsEvent[] {
  if (typeof value === 'string') { try { value = JSON.parse(value) } catch { return [] } }
  if (!Array.isArray(value)) return []
  const shown = new Map<string | number, HmsEvent>()
  for (const [index, event] of value.entries()) {
    if (event?.display === false) continue
    const raw = isCodeWord(event?.raw_code) ? event.raw_code : event?.code
    let key = typeof event?.code === 'string' ? event.code.toUpperCase() : index
    if (isCodeWord(event?.attr) && isCodeWord(raw)) {
      key = [event.attr, raw].map(n => n.toString(16).padStart(8, '0')).join('_').toUpperCase()
    } else if (typeof key === 'number' && isCodeWord(raw)) key = `raw:${raw}`
    if (!shown.has(key) || !isHmsNotice(event) || isHmsNotice(shown.get(key)!)) shown.set(key, event)
  }
  return [...shown.values()]
}
export function hmsFaults(value: unknown): HmsEvent[] {
  return hmsEvents(value).filter(e => !isHmsNotice(e))
}
