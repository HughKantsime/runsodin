import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, Check, X, AlertTriangle, Camera, Filter, ChevronDown } from 'lucide-react'
import { vision, printers } from '../api'
import { canDo } from '../permissions'
import DetectionFeed from '../components/DetectionFeed'

const TYPE_META = {
  spaghetti: { label: 'Spaghetti', color: 'text-red-400', bg: 'bg-red-500/20' },
  first_layer: { label: 'First Layer', color: 'text-amber-400', bg: 'bg-amber-500/20' },
  detachment: { label: 'Detachment', color: 'text-orange-400', bg: 'bg-orange-500/20' },
}

const STATUS_META = {
  pending: { label: 'Pending', color: 'text-yellow-400', bg: 'bg-yellow-500/20' },
  confirmed: { label: 'Confirmed', color: 'text-red-400', bg: 'bg-red-500/20' },
  dismissed: { label: 'Dismissed', color: 'text-farm-400', bg: 'bg-farm-500/20' },
  auto_paused: { label: 'Auto-Paused', color: 'text-purple-400', bg: 'bg-purple-500/20' },
}

function formatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr + (dateStr.includes('Z') ? '' : 'Z'))
  return d.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

function StatBar({ stats }) {
  if (!stats) return null
  const total = Object.values(stats.by_type || {}).reduce((a, b) => a + b, 0)
  return (
    <div className="flex items-center gap-4 mb-4 p-3 bg-farm-900 rounded-lg border border-farm-800 text-sm">
      <div>
        <span className="text-farm-400">Last {stats.days}d:</span>{' '}
        <span className="font-medium">{total} detections</span>
      </div>
      {Object.entries(stats.by_type || {}).map(([type, count]) => (
        <div key={type} className="flex items-center gap-1.5">
          <span className={TYPE_META[type]?.color || 'text-farm-400'}>{TYPE_META[type]?.label || type}</span>
          <span className="text-farm-500">{count}</span>
        </div>
      ))}
      {stats.accuracy_pct != null && (
        <div className="ml-auto">
          <span className="text-farm-400">Accuracy:</span>{' '}
          <span className="font-medium">{stats.accuracy_pct}%</span>
          <span className="text-farm-500 text-xs ml-1">({stats.total_reviewed} reviewed)</span>
        </div>
      )}
    </div>
  )
}

function FrameModal({ detection, onClose, onReview }) {
  const canReview = canDo('manage_printers')
  const [imgEl, setImgEl] = useState(null)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={onClose}>
      <div className="bg-farm-950 rounded-xl border border-farm-700 max-w-3xl w-full mx-4 overflow-hidden" onClick={e => e.stopPropagation()}>
        {/* Frame */}
        <div className="relative bg-black aspect-video">
          {detection.frame_path ? (
            <img
              ref={setImgEl}
              src={`/api/vision/frames/${detection.frame_path}`}
              alt="Detection frame"
              className="w-full h-full object-contain"
            />
          ) : (
            <div className="flex items-center justify-center h-full text-farm-500">
              <Camera size={48} />
            </div>
          )}
          {/* Bounding box overlay */}
          {detection.bbox_json && detection.frame_path && (
            <BboxOverlay bbox={JSON.parse(detection.bbox_json)} imgEl={imgEl} />
          )}
        </div>

        {/* Details */}
        <div className="p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <span className={`px-2 py-0.5 rounded-lg text-xs font-medium ${TYPE_META[detection.detection_type]?.bg} ${TYPE_META[detection.detection_type]?.color}`}>
                {TYPE_META[detection.detection_type]?.label || detection.detection_type}
              </span>
              <span className={`px-2 py-0.5 rounded-lg text-xs font-medium ${STATUS_META[detection.status]?.bg} ${STATUS_META[detection.status]?.color}`}>
                {STATUS_META[detection.status]?.label || detection.status}
              </span>
              <span className="text-sm font-mono text-farm-300">
                {(detection.confidence * 100).toFixed(0)}%
              </span>
            </div>
            <button onClick={onClose} className="text-farm-400 hover:text-white">
              <X size={20} />
            </button>
          </div>

          <div className="text-sm text-farm-400 mb-3">
            <span className="font-medium text-farm-200">{detection.printer_nickname || detection.printer_name}</span>
            <span className="mx-2">&middot;</span>
            {formatDate(detection.created_at)}
          </div>

          {/* Review actions */}
          {canReview && detection.status === 'pending' && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => onReview(detection.id, 'confirmed')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-lg text-sm font-medium transition-colors"
              >
                <AlertTriangle size={14} />
                Confirm Issue
              </button>
              <button
                onClick={() => onReview(detection.id, 'dismissed')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm font-medium transition-colors"
              >
                <X size={14} />
                Dismiss
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function BboxOverlay({ bbox, imgEl }) {
  const [dims, setDims] = useState(null)

  useEffect(() => {
    if (!imgEl) return
    const update = () => {
      if (imgEl.naturalWidth) setDims({ w: imgEl.naturalWidth, h: imgEl.naturalHeight })
    }
    if (imgEl.complete) update()
    imgEl.addEventListener('load', update)
    return () => imgEl.removeEventListener('load', update)
  }, [imgEl])

  if (!bbox || bbox.length !== 4 || !dims) return null
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
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [printerFilter, setPrinterFilter] = useState('')
  const [selectedDetection, setSelectedDetection] = useState(null)
  const [page, setPage] = useState(0)
  const LIMIT = 30

  const { data: statsData } = useQuery({
    queryKey: ['vision-stats'],
    queryFn: () => vision.getStats(7),
    refetchInterval: 30000,
  })

  const params = { limit: LIMIT, offset: page * LIMIT }
  if (typeFilter) params.detection_type = typeFilter
  if (statusFilter) params.status = statusFilter
  if (printerFilter) params.printer_id = printerFilter

  const { data, isLoading } = useQuery({
    queryKey: ['vision-detections', typeFilter, statusFilter, printerFilter, page],
    queryFn: () => vision.getDetections(params),
    refetchInterval: 15000,
  })

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(),
  })

  const items = data?.items || []
  const total = data?.total || 0

  const handleReview = async (id, status) => {
    try {
      await vision.reviewDetection(id, status)
      queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
      queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
      setSelectedDetection(null)
    } catch (e) {
      console.error('Review failed:', e)
    }
  }

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h2 className="text-xl md:text-2xl font-display font-bold flex items-center gap-2">
            <Eye size={24} />
            Vigil AI Detections
          </h2>
          <p className="text-xs md:text-sm text-farm-500 mt-1">
            AI-detected print failures from camera feeds
          </p>
        </div>
      </div>

      <StatBar stats={statsData} />

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select
          value={typeFilter}
          onChange={e => { setTypeFilter(e.target.value); setPage(0) }}
          className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-farm-500"
        >
          <option value="">All Types</option>
          <option value="spaghetti">Spaghetti</option>
          <option value="first_layer">First Layer</option>
          <option value="detachment">Detachment</option>
        </select>

        <select
          value={statusFilter}
          onChange={e => { setStatusFilter(e.target.value); setPage(0) }}
          className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-farm-500"
        >
          <option value="">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="confirmed">Confirmed</option>
          <option value="dismissed">Dismissed</option>
          <option value="auto_paused">Auto-Paused</option>
        </select>

        <select
          value={printerFilter}
          onChange={e => { setPrinterFilter(e.target.value); setPage(0) }}
          className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-farm-500"
        >
          <option value="">All Printers</option>
          {(printersData || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>

        <span className="text-xs text-farm-500 ml-auto">
          {total} detection{total !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Detection grid */}
      {isLoading && (
        <div className="text-center text-farm-500 py-12 text-sm">Loading detections...</div>
      )}

      {!isLoading && items.length === 0 && (
        <div className="text-center text-farm-500 py-12">
          <Eye size={40} className="mx-auto mb-4 text-farm-600" />
          <p className="text-sm">No detections found</p>
          <p className="text-xs mt-1">Detections will appear here when the vision monitor identifies issues</p>
        </div>
      )}

      {items.length > 0 && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {items.map(det => (
              <div
                key={det.id}
                className="bg-farm-950 rounded-lg border border-farm-800 overflow-hidden cursor-pointer hover:border-farm-600 transition-colors"
                onClick={() => setSelectedDetection(det)}
              >
                {/* Thumbnail */}
                <div className="relative aspect-video bg-black">
                  {det.frame_path ? (
                    <img
                      src={`/api/vision/frames/${det.frame_path}`}
                      alt=""
                      className="w-full h-full object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <div className="flex items-center justify-center h-full text-farm-600">
                      <Camera size={24} />
                    </div>
                  )}
                  {/* Confidence badge */}
                  <div className="absolute top-2 right-2 px-1.5 py-0.5 bg-black/70 rounded-lg text-xs font-mono">
                    {(det.confidence * 100).toFixed(0)}%
                  </div>
                </div>

                {/* Info */}
                <div className="p-2.5">
                  <div className="flex items-center justify-between mb-1">
                    <span className={`text-xs font-medium ${TYPE_META[det.detection_type]?.color}`}>
                      {TYPE_META[det.detection_type]?.label || det.detection_type}
                    </span>
                    <span className={`px-1.5 py-0.5 rounded-lg text-[10px] font-medium ${STATUS_META[det.status]?.bg} ${STATUS_META[det.status]?.color}`}>
                      {STATUS_META[det.status]?.label || det.status}
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-xs text-farm-500">
                    <span>{det.printer_nickname || det.printer_name}</span>
                    <span>{formatDate(det.created_at)}</span>
                  </div>
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
                className="px-3 py-1.5 bg-farm-800 rounded-lg text-sm disabled:opacity-40 hover:bg-farm-700 transition-colors"
              >
                Previous
              </button>
              <span className="text-sm text-farm-400">
                Page {page + 1} of {Math.ceil(total / LIMIT)}
              </span>
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={(page + 1) * LIMIT >= total}
                className="px-3 py-1.5 bg-farm-800 rounded-lg text-sm disabled:opacity-40 hover:bg-farm-700 transition-colors"
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
          detection={selectedDetection}
          onClose={() => setSelectedDetection(null)}
          onReview={handleReview}
        />
      )}
    </div>
  )
}
