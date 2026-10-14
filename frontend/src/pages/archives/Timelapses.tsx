import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Film } from 'lucide-react'
import { auth, timelapses, printers } from '../../api'
import { canDo } from '../../permissions'
import type { User, Timelapse } from '../../types'
import ConfirmModal from '../../components/shared/ConfirmModal'
import TimelapseEditor from '../../components/archives/TimelapseEditor'

function audience(user: User | null | undefined) {
  return user && Number.isInteger(user.id) && user.id > 0 ? JSON.stringify([user.id, user.username, user.role, user.group_id]) : ''
}
function durationLabel(seconds: number | null | undefined) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > Number.MAX_SAFE_INTEGER) return null
  const whole = Math.floor(seconds)
  return `${Math.floor(whole / 60)}m ${whole % 60}s`
}
const provenance = 'Historical recordings without verified lifetime provenance cannot be played or deleted. Editing supports verified CFR30 recordings; originals are retained.'
type Selection = { row: Timelapse; etag: string; owner: string; epoch: number }
export default function Timelapses() {
  const client = useQueryClient()
  const { data: user } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const owner = audience(user), current = () => audience(client.getQueryData<User | null>(['auth', 'me'])) === owner && !!owner
  const [printer, setPrinter] = useState(''), [status, setStatus] = useState(''), [offset, setOffset] = useState(0)
  const [selection, setSelection] = useState<Selection | null>(null), [playing, setPlaying] = useState<{ row: Timelapse; owner: string; epoch: number } | null>(null)
  const [editing, setEditing] = useState<{ row: Timelapse; owner: string } | null>(null)
  const [error, setError] = useState(''), [pending, setPending] = useState(false), [confirm, setConfirm] = useState(false)
  const generation = useRef(0), pendingRef = useRef(false), controller = useRef<AbortController | null>(null)
  const limit = 24
  const rows = useQuery({ queryKey: ['timelapses', owner, printer, status, offset], enabled: !!owner, queryFn: () => timelapses.list({ printer_id: printer || undefined, status: status || undefined, limit, offset }) })
  const printerRows = useQuery({ queryKey: ['timelapse-printers', owner], enabled: !!owner, queryFn: () => printers.list() })
  const retire = () => { generation.current++; controller.current?.abort(); controller.current = null; pendingRef.current = false; setPending(false); setSelection(null); setPlaying(null); setConfirm(false) }
  useEffect(() => { retire(); setEditing(null); setError(''); setOffset(0); setPrinter(''); setStatus(''); return () => { generation.current++; controller.current?.abort() } }, [owner])
  const canDelete = user?.role === 'admin' && canDo('printers.edit')
  const canEdit = !!user && ['admin', 'operator'].includes(user.role) && canDo('printers.edit')
  async function prepare(row: Timelapse) {
    if (!current() || !canDelete || !canDo('printers.edit') || pendingRef.current) return
    retire(); const epoch = generation.current; const abort = new AbortController(); controller.current = abort
    setError(''); setPending(true); pendingRef.current = true
    try {
      const detail = await timelapses.detail(row.id, abort.signal)
      if (!current() || epoch !== generation.current || !canDo('printers.edit')) return
      setSelection({ ...detail, owner, epoch }); setConfirm(true)
    } catch (err) { if (current() && epoch === generation.current && !abort.signal.aborted) setError(err instanceof Error ? err.message : 'Unable to verify recording') }
    finally { if (current() && epoch === generation.current) { pendingRef.current = false; setPending(false) } }
  }
  async function remove() {
    const chosen = selection
    if (!chosen || pendingRef.current || !current() || chosen.owner !== owner || chosen.epoch !== generation.current || !canDelete || !canDo('printers.edit')) return
    const epoch = chosen.epoch; const abort = new AbortController(); controller.current = abort
    pendingRef.current = true; setPending(true); setError('')
    try {
      await timelapses.delete(chosen.row.id, chosen.etag, abort.signal)
      if (!current() || epoch !== generation.current) return
      retire(); await client.invalidateQueries({ queryKey: ['timelapses', owner] })
    } catch (err: any) {
      if (!current() || epoch !== generation.current || abort.signal.aborted) return
      if (err?.status === 412) {
        setConfirm(false); setSelection(null)
        setError('The recording changed. Refresh it and confirm deletion again. No retry was sent.')
        await client.invalidateQueries({ queryKey: ['timelapses', owner] })
      } else setError((err?.message || 'Deletion could not be confirmed') + '. Retry uses the same verified lifetime; completion may already have occurred.')
    } finally { if (current() && epoch === generation.current) { pendingRef.current = false; setPending(false) } }
  }
  async function download(row: Timelapse) {
    if (!current() || pendingRef.current) return
    retire(); const epoch = generation.current; const abort = new AbortController(); controller.current = abort; pendingRef.current = true; setPending(true); setError('')
    try {
      await timelapses.checkMedia(row.id, abort.signal)
      if (!current() || epoch !== generation.current) return
      const anchor = document.createElement('a'); anchor.href = timelapses.downloadUrl(row.id); anchor.download = `odin_timelapse_${row.id}.mp4`; document.body.appendChild(anchor); anchor.click(); anchor.remove()
    } catch (err) { if (current() && epoch === generation.current && !abort.signal.aborted) setError(err instanceof Error ? err.message : 'Download unavailable') }
    finally { if (current() && epoch === generation.current) { pendingRef.current = false; setPending(false) } }
  }
  if (!owner) return <div>Sign in to view timelapses.</div>
  const items = rows.data?.timelapses || [], total = rows.data?.total || 0
  const active = selection?.owner === owner && selection.epoch === generation.current ? selection : null
  return <div className="p-4 md:p-6 space-y-4">
    <Link to="/cameras">Back to Cameras</Link><h1 className="text-xl flex items-center gap-2"><Film />Timelapses</h1>
    <p className="text-sm text-[var(--brand-text-muted)]">{provenance}</p>
    <div className="flex gap-3"><select aria-label="Printer filter" value={printer} onChange={e => { retire(); setPrinter(e.target.value); setOffset(0) }}><option value="">All printers</option>{(printerRows.data || []).map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
      <select aria-label="Status filter" value={status} onChange={e => { retire(); setStatus(e.target.value); setOffset(0) }}><option value="">All statuses</option>{['ready', 'capturing', 'encoding', 'failed', 'deleting'].map(value => <option key={value}>{value}</option>)}</select></div>
    {rows.isLoading && <p>Loading timelapses...</p>}{rows.isError && <div role="alert">Unable to load recordings. <button onClick={() => rows.refetch()}>Retry list</button></div>}
    {printerRows.isError && <div role="alert">Unable to load printer filters. <button onClick={() => printerRows.refetch()}>Retry filters</button></div>}
    {error && <div role="alert">{error}</div>}
    {!rows.isLoading && !rows.isError && !items.length && <p>No recordings match these filters.</p>}
    <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">{items.map(row => <article key={row.id} className="border rounded-lg p-4 space-y-2"><h2>{row.printer_name || `Printer ${row.printer_id}`}</h2><p>Recording {row.id}: {row.status}, {row.frame_count} frames</p>
      {row.created_at && <p>{new Date(row.created_at).toLocaleString()}</p>}
      {durationLabel(row.duration_seconds) !== null && <p>{durationLabel(row.duration_seconds)}</p>}
      {Number.isInteger(row.print_job_id) && (row.print_job_id ?? 0) > 0 && <p>Job #{row.print_job_id}</p>}
      {row.status === 'ready' && <><button onClick={() => { retire(); setError(''); setPlaying({ row, owner, epoch: generation.current }) }}>Play recording {row.id}</button><button disabled={pending} onClick={() => download(row)}>Download recording {row.id}</button></>}
      {canEdit && row.status === 'ready' && <button disabled={pending} onClick={() => { if (current()) { retire(); setEditing({ row, owner }) } }}>Edit recording {row.id}</button>}
      {canDelete && ['ready', 'failed', 'deleting'].includes(row.status) && <button disabled={pending} onClick={() => prepare(row)}>Delete recording {row.id}</button>}
    </article>)}</div>
    <div className="flex gap-4"><button disabled={!offset} onClick={() => { retire(); setOffset(Math.max(0, offset - limit)) }}>Previous</button><span>Page {Math.floor(offset / limit) + 1} of {Math.max(1, Math.ceil(total / limit))}</span><button disabled={offset + limit >= total} onClick={() => { retire(); setOffset(offset + limit) }}>Next</button></div>
    {playing?.owner === owner && playing.epoch === generation.current && <section aria-label="Recording player"><button onClick={retire}>Close player</button><video key={`${owner}:${playing.row.id}:${playing.epoch}`} controls autoPlay src={timelapses.streamUrl(playing.row.id)} onError={() => { if (current() && playing.epoch === generation.current) setError('Playback unavailable. Check access and verified lifetime provenance, then retry playback.') }} /><button onClick={() => { if (current()) { const row = playing.row; retire(); setPlaying({ row, owner, epoch: generation.current }); setError('') } }}>Retry playback</button></section>}
    <TimelapseEditor row={editing?.owner === owner ? editing.row : null} onClose={() => setEditing(null)} onComplete={() => { if (current()) client.invalidateQueries({ queryKey: ['timelapses', owner] }) }} />
    <ConfirmModal open={confirm && !!active} pending={pending} title="Delete recording" message="Retire this verified recording and its captured files? This cannot be undone." confirmText="Confirm deletion" onConfirm={remove} onCancel={retire} />
    {active && !pending && error && <button onClick={remove}>Retry deletion</button>}
  </div>
}
