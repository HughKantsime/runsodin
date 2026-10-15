import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Eye, Check, X, AlertTriangle, Camera, Filter, ChevronDown, CheckCircle2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { auth, vision, printers } from '../../api'
import { canDo } from '../../permissions'
import type { User } from '../../types'
import TrainingDataset from '../../components/vision/TrainingDataset'
import BoundedVisionImage from '../../components/vision/BoundedVisionImage'
import { maintenanceAudience, imageBudget } from '../../components/vision/maintenanceMedia'
import { formatDate } from '../../utils/shared'
import { EmptyState } from '../../components/ui'

const TYPE_META = {
  spaghetti: { label: 'Spaghetti', color: 'text-red-400', bg: 'bg-red-500/20' },
  first_layer: { label: 'First Layer', color: 'text-amber-400', bg: 'bg-amber-500/20' },
  detachment: { label: 'Detachment', color: 'text-orange-400', bg: 'bg-orange-500/20' },
  build_plate_empty: { label: 'Plate Ready', color: 'text-green-400', bg: 'bg-green-500/20' },
}

const STATUS_META = {
  pending: { label: 'Pending', color: 'text-yellow-400', bg: 'bg-yellow-500/15' },
  confirmed: { label: 'Confirmed', color: 'text-red-400', bg: 'bg-red-500/15' },
  dismissed: { label: 'Dismissed', color: 'text-[var(--brand-text-muted)]', bg: 'bg-[var(--brand-input-bg)]' },
  auto_paused: { label: 'Auto-Paused', color: 'text-purple-400', bg: 'bg-purple-500/15' },
}

function StatBar({ stats }) {
  if (!stats) return null
  const total = Object.values(stats.by_type || {}).reduce((a, b) => a + b, 0)
  return (
    <div className="flex items-center gap-4 mb-4 p-3 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] text-sm">
      <div>
        <span className="text-[var(--brand-text-muted)]">Last {stats.days}d:</span>{' '}
        <span className="font-medium">{total} detections</span>
      </div>
      {Object.entries(stats.by_type || {}).map(([type, count]) => (
        <div key={type} className="flex items-center gap-1.5">
          <span className={TYPE_META[type]?.color || 'text-[var(--brand-text-muted)]'}>{TYPE_META[type]?.label || type}</span>
          <span className="text-[var(--brand-text-muted)]">{count}</span>
        </div>
      ))}
      {stats.accuracy_pct != null && !isNaN(stats.accuracy_pct) && (
        <div className="ml-auto">
          <span className="text-[var(--brand-text-muted)]">Confirmed review rate:</span>{' '}
          <span className="font-medium">{stats.accuracy_pct}%</span>
          <span className="text-[var(--brand-text-muted)] text-xs ml-1">({stats.total_reviewed} reviewed)</span>
        </div>
      )}
    </div>
  )
}

function FrameModal({ detection, audience, onClose, onReview, onLabel }) {
  const canReview = canDo('printers.edit')
  const [preview, setPreview] = useState(null)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={`Detection details: ${TYPE_META[detection.detection_type]?.label || detection.detection_type}`} className="bg-[var(--brand-content-bg)] rounded-xl border border-[var(--brand-card-border)] max-w-3xl w-full mx-4 overflow-hidden" onClick={e => e.stopPropagation()}>
        {/* Frame */}
        <div className="relative bg-black aspect-video">
          {detection.frame_path ? (
            <BoundedVisionImage path={detection.frame_path} audience={audience} selected alt="Detection frame" className="w-full h-full object-contain" onImage={setPreview} />
          ) : (
            <div className="flex items-center justify-center h-full text-[var(--brand-text-muted)]">
              <Camera size={48} />
            </div>
          )}
          {/* Bounding box overlay */}
          {detection.bbox_json && detection.frame_path && (
            <BboxOverlay bbox={(() => { try { return JSON.parse(detection.bbox_json) } catch { return null } })()} dimensions={preview} />
          )}
        </div>

        {/* Details */}
        <div className="p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <span className={`px-2 py-0.5 rounded-md text-xs font-medium ${TYPE_META[detection.detection_type]?.bg} ${TYPE_META[detection.detection_type]?.color}`}>
                {TYPE_META[detection.detection_type]?.label || detection.detection_type}
              </span>
              <span className={`px-2 py-0.5 rounded-md text-xs font-medium ${STATUS_META[detection.status]?.bg} ${STATUS_META[detection.status]?.color}`}>
                {STATUS_META[detection.status]?.label || detection.status}
              </span>
              <span className="text-sm font-mono text-[var(--brand-text-secondary)]">
                {detection.confidence != null ? (detection.confidence * 100).toFixed(0) + '%' : '-'}
              </span>
            </div>
            <button onClick={onClose} aria-label="Close detection details" className="text-[var(--brand-text-muted)] hover:text-white">
              <X size={20} aria-hidden="true" />
            </button>
          </div>

          <div className="text-sm text-[var(--brand-text-muted)] mb-3">
            <span className="font-medium text-[var(--brand-text-primary)]">{detection.printer_nickname || detection.printer_name}</span>
            <span className="mx-2">&middot;</span>
            {formatDate(detection.created_at)}
          </div>

          {/* Review actions */}
          {canReview && detection.status === 'pending' && (
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => onReview(detection.id, 'confirmed')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-md text-sm font-medium transition-colors"
              >
                <AlertTriangle size={14} />
                Confirm Issue
              </button>
              <button
                onClick={() => onReview(detection.id, 'dismissed')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-card-border)] hover:opacity-80 rounded-md text-sm font-medium transition-colors"
              >
                <X size={14} />
                Dismiss
              </button>

            </div>
          )}
          {canReview && detection.frame_path && <button onClick={() => onLabel(detection.id)} className="rounded border px-3 py-1.5">Label for training</button>}
        </div>
      </div>
    </div>
  )
}

function BboxOverlay({ bbox, dimensions }) {
  const [dims, setDims] = useState(null)

  useEffect(() => { setDims(dimensions ? { w: dimensions.width, h: dimensions.height } : null) }, [dimensions])

  if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some(value => typeof value !== 'number' || !Number.isFinite(value)) || !dims) return null
  const [x1, y1, x2, y2] = bbox
  const sw = Math.max(2, Math.round(dims.w / 200))

  return (
    <svg
      className="absolute inset-0 w-full h-full"
      viewBox={`0 0 ${dims.w} ${dims.h}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ pointerEvents: 'none' }}
    >
      <rect
        x={x1} y={y1}
        width={x2 - x1} height={y2 - y1}
        fill="rgba(239, 68, 68, 0.08)"
        stroke="#ef4444"
        strokeWidth={sw}
        rx={3}
      />
    </svg>
  )
}


export default function Detections() {
  const queryClient = useQueryClient()
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const audience = maintenanceAudience(principal)
  const current = () => !!audience && maintenanceAudience(queryClient.getQueryData<User | null>(['auth', 'me'])) === audience
  const [layout, setLayout] = useState('grid')
  const [dataset, setDataset] = useState<{ audience: string; initialId?: number } | null>(null)
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [printerFilter, setPrinterFilter] = useState('')
  const [selectedDetection, setSelectedDetection] = useState(null)
  const [page, setPage] = useState(0)
  const LIMIT = 30

  const { data: statsData } = useQuery({
    queryKey: ['vision-stats', audience],
    enabled: !!audience,
    queryFn: () => vision.getStats(7),
    refetchInterval: 30000,
  })

  const params = { limit: LIMIT, offset: page * LIMIT }
  if (typeFilter) params.detection_type = typeFilter
  if (statusFilter) params.status = statusFilter
  if (printerFilter) params.printer_id = printerFilter

  const { data, isLoading } = useQuery({
    enabled: !!audience,
    queryKey: ['vision-detections', audience, typeFilter, statusFilter, printerFilter, page],
    queryFn: () => vision.getDetections(params),
    refetchInterval: 15000,
  })

  const { data: printersData } = useQuery({
    queryKey: ['printers', 'vision-maintenance', audience],
    enabled: !!audience,
    queryFn: () => printers.list(),
  })

  const items = data?.items || []
  const total = data?.total || 0

  const [selectedIds, setSelectedIds] = useState(new Set())

  const selectionGeneration = useRef(0)
  const openDetection = (row) => { selectionGeneration.current++; setSelectedDetection(row) }
  const closeDetection = () => { selectionGeneration.current++; setSelectedDetection(null) }
  const handleReview = async (id, status) => {
    const epoch = selectionGeneration.current
    if (!current() || !canDo('printers.edit')) return
    try {
      await vision.reviewDetection(id, status)
      if (!current() || epoch !== selectionGeneration.current) return
      queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
      queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
      setSelectedDetection(null)
    } catch (e) {
      if (!current() || epoch !== selectionGeneration.current) return
      toast.error('Review failed: ' + (e.message || 'Unknown error'))
    }
  }

  // v1.8.8: one-click "false alarm → resume print" flow. Calls the
  // server's dismiss-and-resume endpoint, which marks the detection
  // dismissed AND calls the printer's resume dispatch in a single
  // request. Idempotent on both sides.
  const handleDismissAndResume = async (id) => {
    try {
      const res = await vision.dismissAndResume(id)
      queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
      queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
      queryClient.invalidateQueries({ queryKey: ['printers'] })
      setSelectedDetection(null)
      // Surface what actually happened — the printer may have already
      // been running (nothing to resume) or the resume call may have
      // failed while the dismiss succeeded.
      const acts = (res && res.actions) || []
      const printerAction = acts.find((a) => a.startsWith('printer:')) || ''
      if (printerAction === 'printer:resumed') {
        toast.success('Dismissed and print resumed')
      } else if (printerAction === 'printer:already_running') {
        toast.success('Dismissed (printer was already running)')
      } else if (printerAction === 'printer:resume_failed') {
        toast.error('Dismissed, but resume failed — check the printer')
      } else {
        toast.success('Dismissed')
      }
    } catch (e) {
      toast.error('Action failed: ' + (e.message || 'Unknown error'))
    }
  }

  const handleBulkReview = async (status) => {
    if (!current() || !canDo('printers.edit')) return
    if (selectedIds.size === 0) return
    try {
      await Promise.all([...selectedIds].map(id => vision.reviewDetection(id, status)))
      if (!current()) return
      queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
      queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
      setSelectedIds(new Set())
      toast.success(`${selectedIds.size} detection(s) ${status}`)
    } catch (e) {
      if (!current()) return
      toast.error('Bulk review failed: ' + (e.message || 'Unknown error'))
    }
  }

  const toggleSelect = (id, e) => {
    e.stopPropagation()
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  useEffect(() => { closeDetection(); setSelectedIds(new Set()); setPage(0); setDataset(null); return () => imageBudget.clear(audience) }, [audience])
  if (!audience) return <div className="p-4">Historical Vision records require platform maintenance access. Captured tenant ownership is unavailable.</div>
  if (dataset?.audience === audience) return <div className="p-4 space-y-3"><button onClick={() => setDataset(null)}>Back to detections</button><TrainingDataset key={audience} initialId={dataset.initialId} /></div>
  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <div className="flex items-center gap-3">
            <Eye className="text-[var(--brand-primary)]" size={24} />
            <div>
              <h1 className="text-xl md:text-2xl font-display font-bold">Vigil AI Detections</h1>
              <p className="text-xs md:text-sm text-[var(--brand-text-muted)] mt-1">
                AI-detected print failures from camera feeds
              </p>
            </div>
          </div>
        </div>
      </div>

      <p>Historical owner unknown. These records are available only to platform maintenance.</p>
      <div className="flex gap-2 my-3"><button aria-pressed={layout === 'grid'} onClick={() => setLayout('grid')}>Grid view</button><button aria-pressed={layout === 'list'} onClick={() => setLayout('list')}>List view</button><button onClick={() => { if (current()) setDataset({ audience }) }}>Training dataset</button></div>
      <StatBar stats={statsData} />

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select aria-label="Filter by detection type"
          value={typeFilter}
          onChange={e => { setTypeFilter(e.target.value); setPage(0) }}
          className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm focus:outline-hidden focus:border-[var(--brand-text-muted)]"
        >
          <option value="">All Types</option>
          <option value="spaghetti">Spaghetti</option>
          <option value="first_layer">First Layer</option>
          <option value="detachment">Detachment</option>
          <option value="build_plate_empty">Plate ready</option>
        </select>

        <select aria-label="Filter by status"
          value={statusFilter}
          onChange={e => { setStatusFilter(e.target.value); setPage(0) }}
          className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm focus:outline-hidden focus:border-[var(--brand-text-muted)]"
        >
          <option value="">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="confirmed">Confirmed</option>
          <option value="dismissed">Dismissed</option>
          <option value="auto_paused">Auto-Paused</option>
        </select>

        <select aria-label="Filter by printer"
          value={printerFilter}
          onChange={e => { setPrinterFilter(e.target.value); setPage(0) }}
          className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm focus:outline-hidden focus:border-[var(--brand-text-muted)]"
        >
          <option value="">All Printers</option>
          {(printersData || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>

        <span className="text-xs text-[var(--brand-text-muted)] ml-auto">
          {total} detection{total !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Detection grid */}
      {isLoading && (
        <div className="text-center text-[var(--brand-text-muted)] py-12 text-sm">Loading detections...</div>
      )}

      {!isLoading && items.length === 0 && (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
          <EmptyState
            icon={Eye}
            title="No detections found"
            description="Detections will appear here when the vision monitor identifies issues"
          />
        </div>
      )}

      {/* Bulk action bar */}
      {selectedIds.size > 0 && canDo('printers.edit') && (
        <div className="flex items-center gap-3 mb-4 p-3 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
          <span className="text-sm text-[var(--brand-text-secondary)]">{selectedIds.size} selected</span>
          <button
            onClick={() => handleBulkReview('confirmed')}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-md text-xs font-medium transition-colors"
          >
            <AlertTriangle size={12} /> Confirm All
          </button>
          <button
            onClick={() => handleBulkReview('dismissed')}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-card-border)] hover:opacity-80 rounded-md text-xs font-medium transition-colors"
          >
            <X size={12} /> Dismiss All
          </button>
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-xs text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)] ml-auto"
          >
            Clear selection
          </button>
        </div>
      )}

      {items.length > 0 && (
        <>
          <div className={layout === 'grid' ? "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" : "flex flex-col gap-3"}>
            {items.map(det => (
              <div
                key={det.id}
                className={`bg-[var(--brand-content-bg)] rounded-md border overflow-hidden cursor-pointer hover:border-[var(--brand-text-muted)] transition-colors ${selectedIds.has(det.id) ? 'border-[var(--brand-primary)] ring-1 ring-[var(--brand-primary)]/30' : 'border-[var(--brand-card-border)]'}`}
                onClick={() => openDetection(det)}
              >
                {/* Thumbnail */}
                <div className="relative aspect-video bg-black">
                  {det.frame_path ? (
                    <BoundedVisionImage path={det.frame_path} audience={audience} alt="Detection thumbnail" className="w-full h-full object-cover" />
                  ) : (
                    <div className="flex items-center justify-center h-full text-[var(--brand-text-muted)]">
                      <Camera size={24} />
                    </div>
                  )}
                  {/* Confidence badge */}
                  <div className="absolute top-2 right-2 px-1.5 py-0.5 bg-black/70 rounded-md text-xs font-mono">
                    {det.confidence != null ? (det.confidence * 100).toFixed(0) + '%' : '-'}
                  </div>
                  {/* Checkbox for bulk select */}
                  {canDo('printers.edit') && (
                    <div className="absolute top-2 left-2">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(det.id)}
                        onChange={(e) => toggleSelect(det.id, e)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-4 h-4 rounded border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] text-[var(--brand-primary)] focus:ring-[var(--brand-primary)]"
                      />
                    </div>
                  )}
                </div>

                {/* Info */}
                <div className="p-2.5">
                  <div className="flex items-center justify-between mb-1">
                    <span className={`text-xs font-medium ${TYPE_META[det.detection_type]?.color}`}>
                      {TYPE_META[det.detection_type]?.label || det.detection_type}
                    </span>
                    <span className={`px-1.5 py-0.5 rounded-md text-xs font-medium ${STATUS_META[det.status]?.bg} ${STATUS_META[det.status]?.color}`}>
                      {STATUS_META[det.status]?.label || det.status}
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-xs text-[var(--brand-text-muted)]">
                    <Link
                      to={`/cameras/${det.printer_id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="hover:text-[var(--brand-primary)] transition-colors"
                    >
                      {det.printer_nickname || det.printer_name}
                    </Link>
                    <span>{formatDate(det.created_at)}</span>
                  </div>
                  {/* Inline quick actions for pending items */}
                  {det.status === 'pending' && canDo('printers.edit') && (
                    <div className="flex items-center gap-1.5 mt-2 pt-2 border-t border-[var(--brand-card-border)]">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleReview(det.id, 'confirmed') }}
                        className="flex items-center gap-1 px-2 py-1 bg-red-900/40 hover:bg-red-900/60 rounded text-xs text-red-400 font-medium transition-colors"
                      >
                        <AlertTriangle size={10} /> Confirm
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleReview(det.id, 'dismissed') }}
                        className="flex items-center gap-1 px-2 py-1 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded text-xs text-[var(--brand-text-muted)] font-medium transition-colors"
                      >
                        <X size={10} /> Dismiss
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          {total > LIMIT && (
            <div className="flex items-center justify-center gap-2 mt-4">
              <button
                onClick={() => setPage(p => Math.max(0, p - 1))}
                disabled={page === 0}
                className="px-3 py-1.5 bg-[var(--brand-input-bg)] rounded-md text-sm disabled:opacity-40 hover:bg-[var(--brand-card-border)] transition-colors"
              >
                Previous
              </button>
              <span className="text-sm text-[var(--brand-text-muted)]">
                Page {page + 1} of {Math.ceil(total / LIMIT)}
              </span>
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={(page + 1) * LIMIT >= total}
                className="px-3 py-1.5 bg-[var(--brand-input-bg)] rounded-md text-sm disabled:opacity-40 hover:bg-[var(--brand-card-border)] transition-colors"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {/* Detail modal */}
      {selectedDetection && (
        <FrameModal
          key={audience}
          audience={audience}
          detection={selectedDetection}
          onClose={closeDetection}
          onReview={handleReview}
          onLabel={(id) => { if (current()) { setSelectedDetection(null); setDataset({ audience, initialId: id }) } }}
        />
      )}
    </div>
  )
}
