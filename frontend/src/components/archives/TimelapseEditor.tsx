import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { auth, timelapses } from '../../api'
import type { Timelapse, User } from '../../types'
import { canDo } from '../../permissions'
import type { TimelapseEditReceipt } from '../../api/cameras'
const audience = (u: User | null | undefined) => u && u.id > 0 ? JSON.stringify([u.id, u.username, u.role, u.group_id]) : ''
type Attempt = { operationId: string; etag: string; kind: 'trim' | 'speed'; body: Record<string, number> }
// Mounted even when closed: ambiguity survives close/reopen.
export default function TimelapseEditor({ row, onClose, onComplete }: { row: Timelapse | null; onClose: () => void; onComplete: (r: TimelapseEditReceipt) => void }) {
  const client = useQueryClient()
  const { data: user } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const owner = audience(user), key = owner && row ? `${owner}:${row.id}` : ''
  const video = useRef<HTMLVideoElement | null>(null), previewEnd = useRef<number | null>(null)
  const liveKey = useRef(key); liveKey.current = key
  const epoch = useRef(0), abort = useRef<AbortController | null>(null), attempts = useRef(new Map<string, Attempt>()), busy = useRef(false)
  const [etag, setEtag] = useState(''), [pending, setPending] = useState(false), [error, setError] = useState(''), [needsReload, setNeedsReload] = useState(false)
  const [start, setStart] = useState('0'), [end, setEnd] = useState(''), [speed, setSpeed] = useState('1'), [receipt, setReceipt] = useState<TimelapseEditReceipt | null>(null)
  const permitted = !!user && ['admin', 'operator'].includes(user.role) && canDo('printers.edit')
  const current = (generation: number) => !!owner && liveKey.current === key && canDo('printers.edit') && audience(client.getQueryData<User | null>(['auth', 'me'])) === owner && generation === epoch.current
  useEffect(() => {
    epoch.current++; abort.current?.abort(); busy.current = false
    setPending(false); setError(''); setEtag(''); setReceipt(null); setStart('0'); setEnd(''); setSpeed('1'); setNeedsReload(attempts.current.has(key))
    if (!key || !row || !permitted) return
    const generation = epoch.current, controller = new AbortController(); abort.current = controller; busy.current = true; setPending(true)
    timelapses.detail(row.id, controller.signal).then(detail => { if (current(generation)) { setEtag(detail.etag); if (detail.row.duration_seconds != null) setEnd(String(detail.row.duration_seconds)) } })
      .catch(err => { if (current(generation) && !controller.signal.aborted) setError(err.message || 'Unable to verify recording') })
      .finally(() => { if (current(generation)) { busy.current = false; setPending(false) } })
    return () => { epoch.current++; controller.abort() }
  }, [key, permitted])
  async function reload() {
    if (!row || busy.current || !permitted || !current(epoch.current)) return
    const generation = epoch.current, controller = new AbortController(); abort.current = controller; busy.current = true; setPending(true); setError('')
    try {
      const attempt = attempts.current.get(key)
      if (attempt) {
        try { const result = await timelapses.editReceipt(row.id, attempt.operationId, controller.signal); if (!current(generation)) return; setReceipt(result); onComplete(result) }
        catch (err: any) { if (err?.status !== 404) throw err }
      }
      const detail = await timelapses.detail(row.id, controller.signal)
      if (!current(generation)) return
      setEtag(detail.etag); attempts.current.delete(key); setNeedsReload(false); setError('Reloaded. Review and confirm a new edit.')
    } catch (err: any) { if (current(generation) && !controller.signal.aborted) setError(err.message || 'Previous edit remains unresolved') }
    finally { if (current(generation)) { busy.current = false; setPending(false) } }
  }
  async function submit(kind: 'trim' | 'speed', replay = false) {
    if (!row || busy.current || !permitted || !current(epoch.current) || (!replay && needsReload) || !etag || !canDo('printers.edit')) return
    const generation = epoch.current, controller = new AbortController(); abort.current = controller
    const body: Record<string, number> = kind === 'trim' ? { start_seconds: Number(start), end_seconds: Number(end) } : { multiplier: Number(speed) }
    if (!replay && kind === 'trim' && (!end || !Number.isFinite(body.start_seconds) || !Number.isFinite(body.end_seconds) || body.start_seconds < 0 || body.end_seconds <= body.start_seconds)) { setError('Enter a valid finite time range.'); return }
    const attempt = replay ? attempts.current.get(key) : { operationId: crypto.randomUUID().replaceAll('-', ''), etag, kind, body }
    if (!attempt) return
    attempts.current.set(key, attempt); busy.current = true; setPending(true); setError('')
    try {
      const result = await timelapses.edit(row.id, attempt.kind, attempt.body, attempt.etag, attempt.operationId, controller.signal)
      if (!current(generation)) return
      attempts.current.delete(key); setNeedsReload(false); setReceipt(result); setEtag(''); onComplete(result)
    } catch (err: any) { if (current(generation)) { setNeedsReload(true); setError((err.message || 'Edit could not be confirmed') + '. Reload and review before a new edit; no automatic retry was sent.') } }
    finally { if (current(generation)) { busy.current = false; setPending(false) } }
  }
  if (!row || !owner || !permitted) return null
  return <section role="dialog" aria-label="Edit recording" className="border rounded-lg p-4 space-y-3">
    <h2>Edit recording {row.id}</h2><button onClick={onClose}>Close editor</button><p>Trim publishes a new version and retains the original. Speed creates another recording. Verified CFR30 recordings only.</p>
    {etag && <video ref={video} controls onTimeUpdate={() => { if (video.current && previewEnd.current != null && video.current.currentTime >= previewEnd.current) { video.current.pause(); previewEnd.current = null } }} src={timelapses.streamUrl(row.id, etag)} onError={() => setError('Preview unavailable. Reload the recording.')} />}
    <label>Start seconds <input type="number" min="0" step="any" value={start} disabled={pending} onChange={e => setStart(e.target.value)} /></label>
    <label>End seconds (exclusive) <input type="number" min="0" step="any" value={end} disabled={pending} onChange={e => setEnd(e.target.value)} /></label>
    <button disabled={pending || !etag} onClick={() => { if (video.current && Number.isFinite(Number(start)) && Number(end) > Number(start) && Number(start) >= 0) { video.current.currentTime = Number(start); previewEnd.current = Number(end); video.current.play().catch(() => setError('Preview playback unavailable')) } }}>Preview selected range</button>
    <button disabled={pending || needsReload || !etag} onClick={() => submit('trim')}>Confirm trim</button>
    <label>Speed <select value={speed} disabled={pending} onChange={e => setSpeed(e.target.value)}>{[.5,1,1.5,2,4,8].map(value => <option key={value} value={value}>{value}x</option>)}</select></label>
    <button disabled={pending || needsReload || !etag} onClick={() => submit('speed')}>Create speed recording</button>
    {pending && <p>Verifying and processing recording...</p>}{error && <p role="alert">{error}</p>}
    {needsReload && <><button disabled={pending} onClick={reload}>Reload and review</button><button disabled={pending} onClick={() => submit(attempts.current.get(key)?.kind || 'trim', true)}>Retry exact previous edit</button></>}
    {receipt && <div><p>Edit completed: {receipt.frame_count} frames, {receipt.duration_seconds} seconds.</p>{receipt.playback_available ? <><video controls src={timelapses.streamUrl(receipt.result_tid, `"${receipt.result_nonce}"`)} /><a href={timelapses.downloadUrl(receipt.result_tid, `"${receipt.result_nonce}"`)}>Download edited recording</a></> : <p>This version is no longer current. Refresh the list to open the current recording.</p>}</div>}
  </section>
}
