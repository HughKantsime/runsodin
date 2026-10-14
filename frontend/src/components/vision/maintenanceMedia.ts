import type { User } from '../../types'
export function maintenanceAudience(user: User | null | undefined) {
  return user && Number.isInteger(user.id) && user.id > 0 && user.role === 'admin' && user.group_id === null ? JSON.stringify([user.id, user.username, user.role, user.group_id]) : ''
}
export const MiB = 1024 * 1024
export class ImageBudget {
  encoded = 0; decoded = 0; working = 0
  private entries = new Set<ReturnType<ImageBudget['reserve']>>()
  reserve(owner: string, selected: boolean, evict: () => void) {
    const fits = () => this.encoded + 8 * MiB <= 32 * MiB && this.decoded + 64 * MiB <= 128 * MiB && this.working + 24 * MiB <= 48 * MiB
    if (!fits()) for (const old of [...this.entries]) { if (!old.selected && old.ready) { old.release(); old.evict(); if (fits()) break } }
    if (!fits()) throw new Error('Image budget exhausted. Close a preview or try again.')
    const controller = new AbortController()
    const entry = { owner, selected, evict, ready: false, encoded: 8 * MiB, decoded: 64 * MiB, working: 24 * MiB, url: '', controller,
      touch: () => { if (this.entries.has(entry)) { this.entries.delete(entry); this.entries.add(entry) } },
      release: () => { if (!this.entries.delete(entry)) return; controller.abort(); this.encoded -= entry.encoded; this.decoded -= entry.decoded; this.working -= entry.working; if (entry.url) URL.revokeObjectURL(entry.url) },
      dimensions: (bytes: number, pixels: number) => { if (!this.entries.has(entry)) throw new Error('Preview retired'); this.encoded += bytes - entry.encoded; entry.encoded = bytes; this.decoded += pixels * 4 - entry.decoded; entry.decoded = pixels * 4 },
      publish: (url: string) => { if (!this.entries.has(entry)) { URL.revokeObjectURL(url); throw new Error('Preview retired') }; entry.url = url; entry.ready = true; this.working -= entry.working; entry.working = 0 },
    }
    this.encoded += entry.encoded; this.decoded += entry.decoded; this.working += entry.working; this.entries.add(entry)
    return entry
  }
  clear(owner: string) { for (const entry of [...this.entries]) if (entry.owner === owner) { entry.release(); entry.evict() } }
}
export const imageBudget = new ImageBudget()
export async function boundedBytes(response: Response, max: number, controller: AbortController) {
  if (!response.ok) throw new Error(`Request refused (${response.status})`)
  const declared = response.headers.get('content-length')
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > max)) { controller.abort(); throw new Error('Response exceeds the size limit') }
  if (!response.body) throw new Error('Bounded streaming is unavailable')
  const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0
  const abort = () => { void reader.cancel().catch(() => {}) }; controller.signal.addEventListener('abort', abort)
  try {
    while (true) { if (controller.signal.aborted) throw new Error('Request cancelled'); const { value, done } = await reader.read(); if (done) break; size += value.byteLength; if (size > max) { controller.abort(); throw new Error('Response exceeds the size limit') }; chunks.push(value) }
    if (controller.signal.aborted) throw new Error('Request cancelled')
    const bytes = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length }; return bytes
  } finally { controller.signal.removeEventListener('abort', abort); await reader.cancel().catch(() => {}); reader.releaseLock() }
}
export function jpegDimensions(bytes: Uint8Array) {
  if (bytes.length < 4 || bytes[0] !== 255 || bytes[1] !== 216 || bytes[bytes.length - 2] !== 255 || bytes[bytes.length - 1] !== 217) throw new Error('Invalid JPEG')
  let cursor = 2; let result: { width: number; height: number } | undefined
  while (cursor < bytes.length - 2) {
    if (bytes[cursor++] !== 255) throw new Error('Invalid JPEG header')
    while (bytes[cursor] === 255) cursor++
    const marker = bytes[cursor++]; if (marker === 218) { if (!result) throw new Error('Missing JPEG dimensions'); return result }
    if (cursor + 2 > bytes.length) throw new Error('Truncated JPEG')
    const size = bytes[cursor] * 256 + bytes[cursor + 1]
    if (size < 2 || cursor + size > bytes.length) throw new Error('Truncated JPEG')
    if (marker === 225 && String.fromCharCode(...bytes.slice(cursor + 2, cursor + 6)) === 'Exif') throw new Error('Unsupported JPEG orientation')
    if ([192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207].includes(marker)) {
      if (result || size < 8) throw new Error('Invalid JPEG dimensions')
      const height = bytes[cursor + 3] * 256 + bytes[cursor + 4], width = bytes[cursor + 5] * 256 + bytes[cursor + 6]
      if (!width || !height || width > 4096 || height > 4096 || width * height > 4096 ** 2) throw new Error('JPEG dimensions exceed the limit')
      result = { width, height }
    }
    cursor += size
  }
  throw new Error('Invalid JPEG header')
}
export type Preview = { url: string; digest: string; width: number; height: number }
export async function preview(path: string, lease: ReturnType<ImageBudget['reserve']>, current = () => true): Promise<Preview> {
  if (!/^[1-9][0-9]*\/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}$/.test(path)) throw new Error('Invalid frame path')
  const timer = setTimeout(() => lease.controller.abort(), 10_000)
  try {
    if (!globalThis.crypto?.subtle) {
      lease.release()
      throw new Error('Secure image verification is unavailable. Open ODIN over HTTPS or localhost to view and annotate verified frames.')
    }
    if (!current()) throw new Error('Preview audience changed')
    const response = await fetch('/api/vision/frames/' + path, { credentials: 'include', signal: lease.controller.signal })
    if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'image/jpeg') throw new Error('Expected a JPEG preview')
    const bytes = await boundedBytes(response, 8 * MiB, lease.controller)
    const dimensions = jpegDimensions(bytes); lease.dimensions(bytes.length, dimensions.width * dimensions.height)
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('')
    if (lease.controller.signal.aborted || !current()) throw new Error('Preview cancelled')
    const url = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' })); lease.publish(url)
    return { url, digest, ...dimensions }
  } finally { clearTimeout(timer) }
}
