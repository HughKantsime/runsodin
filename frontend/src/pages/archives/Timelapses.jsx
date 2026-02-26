import { useState, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { timelapses, printers } from '../../api'
import { Film, Trash2, Download, ChevronLeft, ChevronRight, Clock, HardDrive, Loader2, Scissors, Gauge, X } from 'lucide-react'
import ConfirmModal from '../../components/shared/ConfirmModal'

const STATUS_COLORS = {
  ready: 'bg-green-500/20 text-green-400',
  capturing: 'bg-blue-500/20 text-blue-400',
  encoding: 'bg-yellow-500/20 text-yellow-400',
  failed: 'bg-red-500/20 text-red-400',
}

function formatDuration(seconds) {
  if (!seconds) return '-'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const mins = Math.floor(seconds / 60)
  const secs = Math.round(seconds % 60)
  return `${mins}m ${secs}s`
}

function formatSize(mb) {
  if (!mb) return '-'
  if (mb < 1) return `${Math.round(mb * 1024)} KB`
  return `${mb.toFixed(1)} MB`
}

const SPEED_OPTIONS = [0.5, 1, 1.5, 2, 4, 8]

function formatMMSS(sec) {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function TimelapseEditorModal({ timelapse, onClose, onUpdated }) {
  const videoRef = useRef(null)
  const [duration, setDuration] = useState(timelapse.duration_seconds || 0)
  const [trimStart, setTrimStart] = useState(0)
  const [trimEnd, setTrimEnd] = useState(timelapse.duration_seconds || 0)
  const [trimming, setTrimming] = useState(false)
  const [speeding, setSpeeding] = useState(false)
  const [confirmTrim, setConfirmTrim] = useState(false)

  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      const dur = videoRef.current.duration
      if (dur && isFinite(dur)) {
        setDuration(dur)
        setTrimEnd(dur)
      }
    }
  }, [])

  const handleTrimStartChange = (val) => {
    const v = parseFloat(val)
    setTrimStart(v)
    if (videoRef.current) videoRef.current.currentTime = v
  }

  const handleTrimEndChange = (val) => {
    const v = parseFloat(val)
    setTrimEnd(v)
    if (videoRef.current) videoRef.current.currentTime = v
  }

  const handleTrim = async () => {
    setConfirmTrim(false)
    setTrimming(true)
    try {
      await timelapses.trim(timelapse.id, trimStart, trimEnd)
      toast.success('Timelapse trimmed')
      onUpdated()
    } catch (e) {
      const msg = e?.detail || e?.message || 'Trim failed'
      toast.error(msg)
    } finally {
      setTrimming(false)
    }
  }

  const handleSpeed = async (multiplier) => {
    setSpeeding(true)
    try {
      await timelapses.speed(timelapse.id, multiplier)
      toast.success(`${multiplier}x speed timelapse saved`)
      onUpdated()
    } catch (e) {
      const msg = e?.detail || e?.message || 'Speed adjustment failed'
      toast.error(msg)
    } finally {
      setSpeeding(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="max-w-5xl w-full bg-farm-900 rounded-xl border border-farm-700 overflow-hidden" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-farm-800">
          <div className="flex items-center gap-3">
            <Film className="text-print-400" size={20} />
            <span className="font-medium text-farm-200">{timelapse.printer_name}</span>
            {timelapse.created_at && (
              <span className="text-xs text-farm-500">
                {new Date(timelapse.created_at + 'Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-farm-400 hover:text-white"><X size={18} /></button>
        </div>

        {/* Video */}
        <div className="bg-black">
          <video
            ref={videoRef}
            src={timelapses.streamUrl(timelapse.id)}
            controls
            autoPlay
            className="w-full max-h-[50vh]"
            onLoadedMetadata={handleLoadedMetadata}
          />
        </div>

        {/* Editor controls */}
        <div className="p-4 space-y-4">
          {/* Trim */}
          <div className="bg-farm-800 rounded-lg p-3">
            <div className="flex items-center gap-2 mb-3">
              <Scissors size={14} className="text-amber-400" />
              <span className="text-sm font-medium text-farm-200">Trim</span>
            </div>
            <div className="grid grid-cols-2 gap-4 mb-3">
              <div>
                <label className="text-xs text-farm-400 block mb-1">Start ({formatMMSS(trimStart)})</label>
                <input
                  type="range" min={0} max={duration} step={0.1} value={trimStart}
                  onChange={e => handleTrimStartChange(e.target.value)}
                  className="w-full accent-amber-500"
                />
              </div>
              <div>
                <label className="text-xs text-farm-400 block mb-1">End ({formatMMSS(trimEnd)})</label>
                <input
                  type="range" min={0} max={duration} step={0.1} value={trimEnd}
                  onChange={e => handleTrimEndChange(e.target.value)}
                  className="w-full accent-amber-500"
                />
              </div>
            </div>
            <button
              onClick={() => setConfirmTrim(true)}
              disabled={trimming || trimStart >= trimEnd}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
            >
              {trimming ? <Loader2 size={12} className="animate-spin" /> : <Scissors size={12} />}
              Apply Trim ({formatMMSS(trimEnd - trimStart)})
            </button>
          </div>

          {/* Speed */}
          <div className="bg-farm-800 rounded-lg p-3">
            <div className="flex items-center gap-2 mb-3">
              <Gauge size={14} className="text-blue-400" />
              <span className="text-sm font-medium text-farm-200">Speed (saves as new timelapse)</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {SPEED_OPTIONS.map(s => (
                <button
                  key={s}
                  onClick={() => handleSpeed(s)}
                  disabled={speeding}
                  className="px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                >
                  {s}x
                </button>
              ))}
              {speeding && <Loader2 size={14} className="animate-spin text-blue-400 self-center" />}
            </div>
          </div>

          {/* Download */}
          <div className="flex gap-2">
            <a
              href={timelapses.downloadUrl(timelapse.id)}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs font-medium transition-colors"
            >
              <Download size={12} /> Download
            </a>
          </div>
        </div>

        <ConfirmModal
          open={confirmTrim}
          title="Trim Timelapse"
          message="This will overwrite the original timelapse. This cannot be undone. Continue?"
          confirmText="Trim"
          confirmVariant="danger"
          onConfirm={handleTrim}
          onCancel={() => setConfirmTrim(false)}
        />
      </div>
    </div>
  )
}


export default function Timelapses() {
  const queryClient = useQueryClient()
  const [printerFilter, setPrinterFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [playingTimelapse, setPlayingTimelapse] = useState(null)
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  const [editorTimelapse, setEditorTimelapse] = useState(null)
  const limit = 24

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(),
  })

  const { data, isLoading } = useQuery({
    queryKey: ['timelapses', printerFilter, statusFilter, offset],
    queryFn: () => timelapses.list({
      printer_id: printerFilter || undefined,
      status: statusFilter || undefined,
      limit,
      offset,
    }),
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => timelapses.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['timelapses'] })
      setPlayingTimelapse(null)
    },
  })

  const handleBulkDelete = async () => {
    try {
      await Promise.all([...selectedIds].map(id => timelapses.delete(id)))
      queryClient.invalidateQueries({ queryKey: ['timelapses'] })
      toast.success(`${selectedIds.size} timelapse(s) deleted`)
      setSelectedIds(new Set())
    } catch (e) {
      toast.error('Bulk delete failed')
    }
    setConfirmBulkDelete(false)
  }

  const toggleSelect = (id) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const items = data?.timelapses || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / limit)
  const currentPage = Math.floor(offset / limit) + 1

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Film className="text-print-400" size={24} />
          <h1 className="text-xl md:text-2xl font-display font-bold">Timelapses</h1>
          <span className="text-sm px-2 py-0.5 rounded-full bg-farm-700 text-farm-300">{total}</span>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6">
        <select
          value={printerFilter}
          onChange={e => { setPrinterFilter(e.target.value); setOffset(0) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-200"
        >
          <option value="">All Printers</option>
          {(printersData || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={e => { setStatusFilter(e.target.value); setOffset(0) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-200"
        >
          <option value="">All Status</option>
          <option value="ready">Ready</option>
          <option value="capturing">Capturing</option>
          <option value="encoding">Encoding</option>
          <option value="failed">Failed</option>
        </select>
      </div>

      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="flex items-center gap-3 mb-4 p-3 bg-farm-900 rounded-lg border border-farm-800">
          <span className="text-sm text-farm-300">{selectedIds.size} selected</span>
          <button
            onClick={() => setConfirmBulkDelete(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-lg text-xs font-medium transition-colors"
          >
            <Trash2 size={12} /> Delete Selected
          </button>
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-xs text-farm-500 hover:text-farm-300 ml-auto"
          >
            Clear selection
          </button>
        </div>
      )}

      {/* Timelapse Editor Modal */}
      {editorTimelapse && (
        <TimelapseEditorModal
          timelapse={editorTimelapse}
          onClose={() => setEditorTimelapse(null)}
          onUpdated={() => {
            queryClient.invalidateQueries({ queryKey: ['timelapses'] })
            setEditorTimelapse(null)
          }}
        />
      )}

      {/* Video Player Modal */}
      {playingTimelapse && (
        <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4" onClick={() => setPlayingTimelapse(null)}>
          <div className="max-w-4xl w-full" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium text-farm-200">{playingTimelapse.printer_name}</span>
                {playingTimelapse.print_job_id && (
                  <span className="text-xs text-farm-500">Job #{playingTimelapse.print_job_id}</span>
                )}
                {playingTimelapse.created_at && (
                  <span className="text-xs text-farm-500">
                    {new Date(playingTimelapse.created_at + 'Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                  </span>
                )}
              </div>
              <button
                onClick={() => setPlayingTimelapse(null)}
                className="text-farm-400 hover:text-white text-sm"
              >
                Close
              </button>
            </div>
            <video
              src={timelapses.videoUrl(playingTimelapse.id)}
              controls
              autoPlay
              className="w-full rounded-lg"
            />
          </div>
        </div>
      )}

      {/* Grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-farm-400">
          <Loader2 className="animate-spin mr-2" size={20} /> Loading...
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-20 text-farm-500">
          <Film size={48} className="mx-auto mb-4 opacity-50" />
          <p className="text-lg">No timelapses yet</p>
          <p className="text-sm mt-1">Enable timelapse on a printer to start recording</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {items.map(t => (
            <div key={t.id} className={`bg-farm-800 rounded-xl border overflow-hidden group ${selectedIds.has(t.id) ? 'border-print-500 ring-1 ring-print-500/30' : 'border-farm-700'}`}>
              {/* Thumbnail / Play area */}
              <div
                className="relative aspect-video bg-farm-900 flex items-center justify-center cursor-pointer hover:bg-farm-800 transition-colors"
                onClick={() => t.status === 'ready' && setPlayingTimelapse(t)}
              >
                {/* Checkbox for bulk select */}
                {t.status === 'ready' && (
                  <div className="absolute top-2 left-2 z-10">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(t.id)}
                      onChange={() => toggleSelect(t.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="w-4 h-4 rounded border-farm-600 bg-farm-900 text-print-500 focus:ring-print-500"
                    />
                  </div>
                )}
                {t.status === 'ready' ? (
                  <>
                    <Film size={40} className="text-farm-600 group-hover:text-amber-400 transition-colors" />
                    <div className="absolute bottom-2 right-2 bg-black/70 text-white text-xs px-2 py-0.5 rounded">
                      {formatDuration(t.duration_seconds)}
                    </div>
                  </>
                ) : t.status === 'capturing' ? (
                  <div className="flex items-center gap-2 text-blue-400">
                    <div className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                    <span className="text-sm">Recording ({t.frame_count} frames)</span>
                  </div>
                ) : t.status === 'encoding' ? (
                  <div className="flex items-center gap-2 text-yellow-400">
                    <Loader2 className="animate-spin" size={18} />
                    <span className="text-sm">Encoding...</span>
                  </div>
                ) : (
                  <span className="text-red-400 text-sm">Failed</span>
                )}
              </div>

              {/* Info */}
              <div className="p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium text-farm-200 truncate">{t.printer_name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${STATUS_COLORS[t.status] || ''}`}>
                    {t.status}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-farm-400">
                  {t.frame_count > 0 && <span>{t.frame_count} frames</span>}
                  {t.file_size_mb > 0 && (
                    <span className="flex items-center gap-1"><HardDrive size={10} />{formatSize(t.file_size_mb)}</span>
                  )}
                  {t.created_at && (
                    <span className="flex items-center gap-1">
                      <Clock size={10} />
                      {new Date(t.created_at + 'Z').toLocaleDateString()}
                    </span>
                  )}
                </div>
                {t.print_job_id && (
                  <div className="text-[10px] text-farm-500 mt-1">Job #{t.print_job_id}</div>
                )}

                {/* Actions */}
                {t.status === 'ready' && (
                  <div className="flex items-center gap-2 mt-2 pt-2 border-t border-farm-700">
                    <button
                      onClick={() => setEditorTimelapse(t)}
                      className="flex items-center gap-1 text-xs text-farm-400 hover:text-print-400 transition-colors"
                    >
                      <Scissors size={12} /> Edit
                    </button>
                    <a
                      href={timelapses.downloadUrl(t.id)}
                      className="flex items-center gap-1 text-xs text-farm-400 hover:text-amber-400 transition-colors"
                    >
                      <Download size={12} /> Download
                    </a>
                    <button
                      onClick={() => setConfirmDeleteId(t.id)}
                      className="flex items-center gap-1 text-xs text-farm-400 hover:text-red-400 transition-colors ml-auto"
                    >
                      <Trash2 size={12} /> Delete
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-4 mt-6">
          <button
            onClick={() => setOffset(Math.max(0, offset - limit))}
            disabled={offset === 0}
            className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-farm-800 text-sm text-farm-300 hover:bg-farm-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={14} /> Previous
          </button>
          <span className="text-sm text-farm-400">Page {currentPage} of {totalPages}</span>
          <button
            onClick={() => setOffset(offset + limit)}
            disabled={currentPage >= totalPages}
            className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-farm-800 text-sm text-farm-300 hover:bg-farm-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Next <ChevronRight size={14} />
          </button>
        </div>
      )}

      <ConfirmModal
        open={!!confirmDeleteId}
        title="Delete Timelapse"
        message="Are you sure you want to delete this timelapse? This cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
        onConfirm={() => { deleteMutation.mutate(confirmDeleteId); setConfirmDeleteId(null) }}
        onCancel={() => setConfirmDeleteId(null)}
      />
      <ConfirmModal
        open={confirmBulkDelete}
        title="Delete Timelapses"
        message={`Are you sure you want to delete ${selectedIds.size} timelapse(s)? This cannot be undone.`}
        confirmText="Delete All"
        confirmVariant="danger"
        onConfirm={handleBulkDelete}
        onCancel={() => setConfirmBulkDelete(false)}
      />
    </div>
  )
}
