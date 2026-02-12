import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { timelapses, printers } from '../api'
import { Film, Trash2, Download, ChevronLeft, ChevronRight, Clock, HardDrive, Loader2 } from 'lucide-react'

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

export default function Timelapses() {
  const queryClient = useQueryClient()
  const [printerFilter, setPrinterFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [playingId, setPlayingId] = useState(null)
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
      setPlayingId(null)
    },
  })

  const items = data?.timelapses || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / limit)
  const currentPage = Math.floor(offset / limit) + 1

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Film className="text-amber-400" size={24} />
          <h1 className="text-2xl font-bold" style={{ color: 'var(--brand-text-primary)' }}>Timelapses</h1>
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

      {/* Video Player Modal */}
      {playingId && (
        <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4" onClick={() => setPlayingId(null)}>
          <div className="max-w-4xl w-full" onClick={e => e.stopPropagation()}>
            <video
              src={timelapses.videoUrl(playingId)}
              controls
              autoPlay
              className="w-full rounded-lg"
            />
            <div className="flex justify-end mt-2">
              <button
                onClick={() => setPlayingId(null)}
                className="text-farm-400 hover:text-white text-sm"
              >
                Close
              </button>
            </div>
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
            <div key={t.id} className="bg-farm-800 rounded-xl border border-farm-700 overflow-hidden group">
              {/* Thumbnail / Play area */}
              <div
                className="relative aspect-video bg-farm-900 flex items-center justify-center cursor-pointer hover:bg-farm-800 transition-colors"
                onClick={() => t.status === 'ready' && setPlayingId(t.id)}
              >
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
                    <a
                      href={timelapses.videoUrl(t.id)}
                      download
                      className="flex items-center gap-1 text-xs text-farm-400 hover:text-amber-400 transition-colors"
                    >
                      <Download size={12} /> Download
                    </a>
                    <button
                      onClick={() => { if (confirm('Delete this timelapse?')) deleteMutation.mutate(t.id) }}
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
    </div>
  )
}
