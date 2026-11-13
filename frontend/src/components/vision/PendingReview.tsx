import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Camera, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { vision } from '../../api'
import type { VisionDetection } from '../../api/vision'
import { canDo } from '../../permissions'
import { DialogSurface } from '../ui'
import { formatDate } from '../../utils/shared'
import BoundedVisionImage from './BoundedVisionImage'
import {
  appendItems, back, createQueue, currentItem, hasPrevious, isDone, labelItem, labelOf,
  needsMore, nextOffset, progress, progressText, skip, type ReviewLabel, type ReviewQueue,
} from './reviewQueue'

const PAGE = 30

// Same labels the Detections page shows for each detection type.
const TYPE_LABEL: Record<string, string> = {
  spaghetti: 'Spaghetti',
  first_layer: 'First Layer',
  detachment: 'Detachment',
  build_plate_empty: 'Plate Ready',
}

const KBD = 'ml-2 px-1.5 py-0.5 rounded border border-white/30 text-[10px] font-mono leading-none'

function Overlay({ bbox, dims }: { bbox: unknown; dims: { width: number; height: number } | null }) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some(v => typeof v !== 'number' || !Number.isFinite(v)) || !dims) return null
  const [x1, y1, x2, y2] = bbox as number[]
  return (
    <svg className="absolute inset-0 w-full h-full" viewBox={`0 0 ${dims.width} ${dims.height}`} preserveAspectRatio="xMidYMid meet" style={{ pointerEvents: 'none' }} aria-hidden="true">
      <rect x={x1} y={y1} width={x2 - x1} height={y2 - y1} fill="rgba(239, 68, 68, 0.08)" stroke="#ef4444" strokeWidth={Math.max(2, Math.round(dims.width / 200))} rx={3} />
    </svg>
  )
}

function ReviewDialog({ audience, isCurrent, onClose }: { audience: string; isCurrent: () => boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const titleId = useId()
  const [queue, setQueue] = useState<ReviewQueue<VisionDetection> | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState(false)
  const [announce, setAnnounce] = useState('')
  const [preview, setPreview] = useState<{ width: number; height: number } | null>(null)
  const busyRef = useRef(false)
  const loadingRef = useRef(false)
  const queueRef = useRef(queue)
  queueRef.current = queue
  const bodyRef = useRef<HTMLDivElement>(null)

  const loadPage = useCallback(async () => {
    if (loadingRef.current) return
    loadingRef.current = true
    setLoading(true)
    setLoadError('')
    try {
      const res = await vision.getDetections({ status: 'pending', limit: PAGE, offset: nextOffset(queueRef.current) })
      if (!isCurrent()) return
      setQueue(prev => (prev ? appendItems(prev, res.items || [], res.total || 0) : createQueue(res.items || [], res.total || 0)))
    } catch (e: any) {
      if (isCurrent()) setLoadError(e?.message || 'Unknown error')
    } finally {
      loadingRef.current = false
      setLoading(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { void loadPage() }, [loadPage])
  useEffect(() => {
    if (queue && !loadError && needsMore(queue) && !loadingRef.current) void loadPage()
  }, [queue, loadError, loadPage])

  // Refresh the page list once, when the reviewer closes.
  const closedRef = useRef(false)
  useEffect(() => {
    closedRef.current = false
    return () => {
      closedRef.current = true
      if ((queueRef.current?.reviewed || 0) > 0) {
        queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
        queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
      }
    }
  }, [queryClient])

  const act = async (label: ReviewLabel) => {
    const q = queueRef.current
    const det = q ? currentItem(q) : undefined
    if (!det || busyRef.current) return
    if (!isCurrent() || !canDo('printers.edit')) return
    busyRef.current = true
    setBusy(true)
    try {
      await vision.reviewDetection(det.id, label)
      if (closedRef.current) {
        // Labelled after the reviewer closed: the page list must still learn about it.
        queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
        queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
        return
      }
      if (!isCurrent()) return
      // Advance the ref in the same tick as the state: a key pressed before the next render must
      // act on the NEXT detection, never label this one a second time.
      const advanced = queueRef.current ? labelItem(queueRef.current, det.id, label) : queueRef.current
      queueRef.current = advanced
      setQueue(advanced)
      const msg = label === 'confirmed' ? 'Confirmed as a real failure' : 'Dismissed as a false alarm'
      setAnnounce(`${msg}: ${det.printer_nickname || det.printer_name || 'printer ' + det.printer_id}`)
      toast.success(msg)
      bodyRef.current?.focus()
    } catch (e: any) {
      if (isCurrent()) toast.error('Review failed: ' + (e?.message || 'Unknown error'))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  const actRef = useRef(act)
  actRef.current = act

  const move = (fn: (q: ReviewQueue<VisionDetection>) => ReviewQueue<VisionDetection>) => {
    if (busyRef.current) return
    setQueue(prev => (prev ? fn(prev) : prev))
  }
  const moveRef = useRef(move)
  moveRef.current = move

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (busyRef.current) return
      switch (e.key) {
        case 'f': case 'F': e.preventDefault(); void actRef.current('confirmed'); break
        case 'j': case 'J': e.preventDefault(); void actRef.current('dismissed'); break
        case 's': case 'S': case 'ArrowRight': e.preventDefault(); moveRef.current(skip); break
        case 'ArrowLeft': e.preventDefault(); moveRef.current(back); break
        default: break
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const det = queue ? currentItem(queue) : undefined
  const done = !!queue && !det && isDone(queue)
  const p = queue ? progress(queue) : null
  const name = det ? det.printer_nickname || det.printer_name || `Printer ${det.printer_id}` : ''
  const typeLabel = det ? TYPE_LABEL[det.detection_type] || det.detection_type : ''
  const existing = det && queue ? labelOf(queue, det.id) : undefined
  let bbox: unknown = null
  if (det?.bbox_json) { try { bbox = JSON.parse(det.bbox_json) } catch { bbox = null } }
  const repeatGuard = (e: React.KeyboardEvent) => { if (e.repeat) e.preventDefault() }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <DialogSurface role="dialog" aria-modal="true" aria-labelledby={titleId} onEscape={onClose} className="bg-[var(--brand-content-bg)] rounded-xl border border-[var(--brand-card-border)] max-w-5xl w-full mx-4 max-h-[95vh] overflow-auto">
        <div ref={bodyRef} tabIndex={-1} className="p-4 outline-hidden">
          <div className="flex items-center justify-between gap-3 mb-3">
            <div>
              <h2 id={titleId} className="text-lg font-display font-bold">Review pending detections</h2>
              {p && <p className="text-sm text-[var(--brand-text-muted)]">{progressText(p)}</p>}
            </div>
            <button type="button" onClick={onClose} aria-label="Exit review" aria-keyshortcuts="Escape" className="flex items-center text-[var(--brand-text-muted)] hover:text-white">
              <X size={20} aria-hidden="true" /><span aria-hidden="true" className={KBD}>Esc</span>
            </button>
          </div>

          <p role="status" className="sr-only">{announce}</p>

          {loading && !det && <p role="status" className="py-12 text-center text-sm text-[var(--brand-text-muted)]">Loading pending detections...</p>}

          {loadError && (
            <p role="alert" className="py-4 text-sm text-red-400">
              Could not load pending detections: {loadError}{' '}
              <button type="button" className="underline" onClick={() => void loadPage()}>Retry</button>
            </p>
          )}

          {done && <p className="py-12 text-center text-sm">No more pending detections to review.{hasPrevious(queue!) ? ' Use Back to revisit earlier ones.' : ''}</p>}

          {det && (
            <>
              <div className="relative bg-black aspect-video">
                {det.frame_path ? (
                  <BoundedVisionImage key={det.id} path={det.frame_path} audience={audience} selected alt={`Detection frame from ${name}: ${typeLabel}`} className="w-full h-full object-contain" onImage={setPreview} />
                ) : (
                  <div className="flex items-center justify-center h-full text-[var(--brand-text-muted)]"><Camera size={48} aria-hidden="true" /></div>
                )}
                {det.frame_path && <Overlay bbox={bbox} dims={preview} />}
              </div>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 my-3 text-sm">
                <span className="font-medium">{name}</span>
                <span>{typeLabel}</span>
                <span className="font-mono">{det.confidence != null ? (det.confidence * 100).toFixed(0) + '%' : '-'}</span>
                <span className="text-[var(--brand-text-muted)]">{formatDate(det.created_at)}</span>
                {existing && <span className="text-[var(--brand-text-muted)]">Already labelled: {existing}</span>}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <button type="button" disabled={busy} aria-keyshortcuts="F" onKeyDown={repeatGuard} onClick={() => void act('confirmed')} className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-md text-sm font-medium transition-colors disabled:opacity-50">
                  <AlertTriangle size={14} aria-hidden="true" />Confirm Issue<span aria-hidden="true" className={KBD}>F</span>
                </button>
                <button type="button" disabled={busy} aria-keyshortcuts="J" onKeyDown={repeatGuard} onClick={() => void act('dismissed')} className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-card-border)] hover:opacity-80 rounded-md text-sm font-medium transition-colors disabled:opacity-50">
                  <X size={14} aria-hidden="true" />Dismiss<span aria-hidden="true" className={KBD}>J</span>
                </button>
                <button type="button" disabled={busy} aria-keyshortcuts="S ArrowRight" onKeyDown={repeatGuard} onClick={() => move(skip)} className="px-3 py-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm transition-colors disabled:opacity-50">
                  Skip<span aria-hidden="true" className={KBD}>S</span>
                </button>
              </div>
            </>
          )}

          {queue && (
            <div className="flex items-center gap-2 mt-3">
              <button type="button" disabled={busy || !hasPrevious(queue)} aria-keyshortcuts="ArrowLeft" onClick={() => move(back)} className="px-3 py-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm transition-colors disabled:opacity-40">
                Back<span aria-hidden="true" className={KBD}>←</span>
              </button>
            </div>
          )}

          <p className="mt-3 text-xs text-[var(--brand-text-muted)]">F confirm · J dismiss · S or → skip · ← back · Esc exit</p>
        </div>
      </DialogSurface>
    </div>
  )
}

/**
 * "Review pending" entry point for the Detections page. Renders nothing unless
 * the user can review (same canDo check as the existing buttons) and there is
 * at least one pending detection.
 */
export default function PendingReview({ audience, current }: { audience: string; current: () => boolean }) {
  const canReview = canDo('printers.edit')
  const [open, setOpen] = useState(false)
  const { data } = useQuery({
    enabled: !!audience && canReview,
    queryKey: ['vision-detections', audience, 'pending-probe'],
    queryFn: () => vision.getDetections({ status: 'pending', limit: 1, offset: 0 }),
    refetchInterval: 15000,
  })
  useEffect(() => { setOpen(false) }, [audience])

  const pending = data?.total || 0
  if (!canReview || (!open && pending === 0)) return null
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Review pending{pending > 0 ? ` (${pending})` : ''}</button>
      {open && <ReviewDialog audience={audience} isCurrent={current} onClose={() => setOpen(false)} />}
    </>
  )
}
