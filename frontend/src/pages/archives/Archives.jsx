import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { archives, printers as printersApi } from '../../api'
import { canDo } from '../../permissions'
import { Archive, Search, X, Trash2, Clock, ChevronLeft, ChevronRight, GitCompare, Tag, RotateCcw } from 'lucide-react'
import toast from 'react-hot-toast'

const STATUS_BADGES = {
  completed: 'bg-green-600/20 text-green-400',
  failed: 'bg-red-600/20 text-red-400',
  cancelled: 'bg-yellow-600/20 text-yellow-400',
}

function formatDuration(seconds) {
  if (!seconds) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

function formatDate(d) {
  if (!d) return '--'
  return new Date(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function TagChips({ tags, onTagClick }) {
  if (!tags || tags.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map(t => (
        <button
          key={t}
          onClick={e => { e.stopPropagation(); onTagClick?.(t) }}
          className="px-1.5 py-0.5 bg-print-600/20 text-print-400 rounded text-[10px] hover:bg-print-600/30 transition-colors"
        >
          {t}
        </button>
      ))}
    </div>
  )
}

function TagEditor({ tags, onChange }) {
  const [input, setInput] = useState('')

  const addTag = () => {
    const tag = input.trim()
    if (tag && !tags.includes(tag)) {
      onChange([...tags, tag])
    }
    setInput('')
  }

  return (
    <div>
      <label className="block text-xs text-farm-500 mb-1">Tags</label>
      <div className="flex flex-wrap gap-1 mb-2">
        {tags.map(t => (
          <span key={t} className="flex items-center gap-1 px-2 py-0.5 bg-print-600/20 text-print-400 rounded text-xs">
            {t}
            <button onClick={() => onChange(tags.filter(x => x !== t))} className="hover:text-red-400">
              <X size={10} />
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-1">
        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }}
          placeholder="Add tag..."
          className="flex-1 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs"
        />
        <button onClick={addTag} className="px-2 py-1 bg-print-600 hover:bg-print-700 rounded text-xs">Add</button>
      </div>
    </div>
  )
}

function CompareModal({ a, b, onClose }) {
  const { data, isLoading } = useQuery({
    queryKey: ['archive-compare', a.id, b.id],
    queryFn: () => archives.compare(a.id, b.id),
  })

  const fields = [
    { key: 'printer_display', label: 'Printer' },
    { key: 'status', label: 'Status' },
    { key: 'actual_duration_seconds', label: 'Duration', fmt: formatDuration },
    { key: 'filament_used_grams', label: 'Filament (g)', fmt: v => v ? `${v.toFixed(1)}g` : '--' },
    { key: 'cost_estimate', label: 'Cost', fmt: v => v ? `$${v.toFixed(2)}` : '--' },
    { key: 'started_at', label: 'Started', fmt: formatDate },
    { key: 'completed_at', label: 'Completed', fmt: formatDate },
  ]

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-farm-900 rounded-xl border border-farm-800 w-full max-w-2xl p-4 sm:p-6 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold flex items-center gap-2"><GitCompare size={18} /> Compare Archives</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        {isLoading ? (
          <div className="text-center py-8 text-farm-500">Loading...</div>
        ) : data ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-farm-400 text-xs border-b border-farm-800">
                  <th className="py-2 px-3 text-left">Field</th>
                  <th className="py-2 px-3 text-left truncate max-w-[200px]">{data.a.print_name}</th>
                  <th className="py-2 px-3 text-left truncate max-w-[200px]">{data.b.print_name}</th>
                </tr>
              </thead>
              <tbody>
                {fields.map(f => {
                  const isDiff = data.diff && f.key in data.diff
                  const fmt = f.fmt || (v => v ?? '--')
                  return (
                    <tr key={f.key} className={`border-t border-farm-800 ${isDiff ? 'bg-amber-900/20' : ''}`}>
                      <td className="py-2 px-3 text-farm-400">{f.label}</td>
                      <td className="py-2 px-3">{fmt(data.a[f.key])}</td>
                      <td className="py-2 px-3">{fmt(data.b[f.key])}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function ArchiveDetail({ archive, onClose, onDelete, onNotesUpdate, onTagsUpdate, printerList }) {
  const [notes, setNotes] = useState(archive.notes || '')
  const [saving, setSaving] = useState(false)
  const [archiveTags, setArchiveTags] = useState(archive.tags || [])
  const [showReprint, setShowReprint] = useState(false)
  const [reprintPrinterId, setReprintPrinterId] = useState('')
  const [reprintPlate, setReprintPlate] = useState(0)
  const [reprinting, setReprinting] = useState(false)

  const plateCount = archive.plate_count || 1

  const handleReprint = async () => {
    if (!reprintPrinterId) return toast.error('Select a printer')
    setReprinting(true)
    try {
      const result = await archives.reprint(archive.id, {
        printer_id: parseInt(reprintPrinterId),
        plate_index: reprintPlate,
      })
      toast.success(result.message || 'Reprint job created')
      setShowReprint(false)
    } catch (e) {
      toast.error(e.message || 'Reprint failed')
    }
    setReprinting(false)
  }

  const saveNotes = async () => {
    setSaving(true)
    try {
      await onNotesUpdate(archive.id, notes)
      toast.success('Notes saved')
    } catch { toast.error('Failed to save') }
    setSaving(false)
  }

  const saveTags = async (newTags) => {
    setArchiveTags(newTags)
    try {
      await onTagsUpdate(archive.id, newTags)
      toast.success('Tags saved')
    } catch { toast.error('Failed to save tags') }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-xl border border-farm-800 w-full max-w-lg p-4 sm:p-6 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold truncate">{archive.print_name}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        {/* Thumbnail */}
        {archive.thumbnail_b64 && (
          <div className="mb-4 rounded-lg overflow-hidden bg-black flex items-center justify-center" style={{ maxHeight: '200px' }}>
            <img src={`data:image/png;base64,${archive.thumbnail_b64}`} alt="Thumbnail" className="max-h-[200px] object-contain" />
          </div>
        )}

        {/* Details grid */}
        <div className="grid grid-cols-2 gap-3 text-sm mb-4">
          <div>
            <span className="text-farm-500 text-xs">Printer</span>
            <p>{archive.printer_display}</p>
          </div>
          <div>
            <span className="text-farm-500 text-xs">Status</span>
            <p><span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[archive.status] || 'bg-farm-800 text-farm-400'}`}>{archive.status}</span></p>
          </div>
          <div>
            <span className="text-farm-500 text-xs">Started</span>
            <p className="text-xs">{formatDate(archive.started_at)}</p>
          </div>
          <div>
            <span className="text-farm-500 text-xs">Completed</span>
            <p className="text-xs">{formatDate(archive.completed_at)}</p>
          </div>
          <div>
            <span className="text-farm-500 text-xs">Duration</span>
            <p>{formatDuration(archive.actual_duration_seconds)}</p>
          </div>
          <div>
            <span className="text-farm-500 text-xs">Filament</span>
            <p>{archive.filament_used_grams ? `${archive.filament_used_grams.toFixed(1)}g` : '--'}</p>
          </div>
        </div>

        {/* Tags */}
        {canDo('printers.edit') ? (
          <div className="mb-4">
            <TagEditor tags={archiveTags} onChange={saveTags} />
          </div>
        ) : archiveTags.length > 0 ? (
          <div className="mb-4">
            <label className="block text-xs text-farm-500 mb-1">Tags</label>
            <TagChips tags={archiveTags} />
          </div>
        ) : null}

        {/* Notes */}
        {canDo('printers.edit') && (
          <div className="mb-4">
            <label className="block text-xs text-farm-500 mb-1">Notes</label>
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              rows={3}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm resize-none"
              placeholder="Add notes..."
            />
            <button
              onClick={saveNotes}
              disabled={saving}
              className="mt-1 px-3 py-1 bg-print-600 hover:bg-print-700 rounded text-xs disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save Notes'}
            </button>
          </div>
        )}

        {/* Reprint */}
        {canDo('printers.edit') && archive.file_path && (
          <div className="mb-4">
            {!showReprint ? (
              <button
                onClick={() => setShowReprint(true)}
                className="w-full py-2 rounded-lg text-sm text-print-400 bg-print-600/10 hover:bg-print-600/20 transition-colors flex items-center justify-center gap-2"
              >
                <RotateCcw size={14} /> Reprint
              </button>
            ) : (
              <div className="bg-farm-800 border border-farm-700 rounded-lg p-3 space-y-3">
                <label className="block text-xs text-farm-500">Target Printer</label>
                <select
                  value={reprintPrinterId}
                  onChange={e => setReprintPrinterId(e.target.value)}
                  className="w-full bg-farm-900 border border-farm-700 rounded px-2 py-1.5 text-sm"
                >
                  <option value="">Select printer...</option>
                  {(printerList || []).filter(p => p.is_active).map(p => (
                    <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
                  ))}
                </select>
                {plateCount > 1 && (
                  <>
                    <label className="block text-xs text-farm-500">Plate</label>
                    <div className="flex flex-wrap gap-2">
                      {Array.from({ length: plateCount }, (_, i) => (
                        <button
                          key={i}
                          onClick={() => setReprintPlate(i)}
                          className={`px-3 py-1 rounded text-xs border transition-colors ${
                            reprintPlate === i
                              ? 'border-print-500 bg-print-900/30 text-print-400'
                              : 'border-farm-700 text-farm-400 hover:border-farm-500'
                          }`}
                        >
                          Plate {i + 1}
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowReprint(false)}
                    className="flex-1 py-1.5 rounded text-xs bg-farm-700 hover:bg-farm-600 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleReprint}
                    disabled={reprinting || !reprintPrinterId}
                    className="flex-1 py-1.5 rounded text-xs bg-print-600 hover:bg-print-500 transition-colors disabled:opacity-50"
                  >
                    {reprinting ? 'Creating...' : 'Reprint'}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Delete */}
        {canDo('printers.delete') && (
          <button
            onClick={() => { onDelete(archive.id); onClose() }}
            className="w-full py-2 rounded-lg text-sm text-red-400 bg-red-600/10 hover:bg-red-600/20 transition-colors"
          >
            Delete Archive
          </button>
        )}
      </div>
    </div>
  )
}

export default function ArchivesPage() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [filterPrinter, setFilterPrinter] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterTag, setFilterTag] = useState('')
  const [selected, setSelected] = useState(null)
  const [compareIds, setCompareIds] = useState(new Set())
  const [showCompare, setShowCompare] = useState(false)

  const { data: printerList } = useQuery({
    queryKey: ['printers-list'],
    queryFn: () => printersApi.list(),
  })

  const { data, isLoading } = useQuery({
    queryKey: ['archives', page, search, filterPrinter, filterStatus, filterTag],
    queryFn: () => archives.list({
      page,
      per_page: 50,
      search: search || undefined,
      printer_id: filterPrinter || undefined,
      status: filterStatus || undefined,
      tag: filterTag || undefined,
    }),
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => archives.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] })
      toast.success('Archive deleted')
    },
  })

  const toggleCompare = (id) => {
    setCompareIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else if (next.size < 2) next.add(id)
      return next
    })
  }

  const items = data?.items || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / 50) || 1
  const compareArr = [...compareIds]

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center gap-3 mb-4 md:mb-6">
        <Archive className="text-print-400" size={24} />
        <div>
          <h1 className="text-xl md:text-2xl font-display font-bold">Print Archive</h1>
          <p className="text-farm-500 text-sm mt-1">{total} archived prints</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { setSearch(searchInput); setPage(1) } }}
            placeholder="Search prints..."
            className="w-full pl-9 pr-3 py-2 bg-farm-800 border border-farm-700 rounded-lg text-sm"
          />
        </div>
        <select
          value={filterPrinter}
          onChange={e => { setFilterPrinter(e.target.value); setPage(1) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-2 text-sm"
        >
          <option value="">All Printers</option>
          {(printerList || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>
        <select
          value={filterStatus}
          onChange={e => { setFilterStatus(e.target.value); setPage(1) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        {filterTag && (
          <button
            onClick={() => { setFilterTag(''); setPage(1) }}
            className="flex items-center gap-1 px-2 py-2 bg-print-600/20 text-print-400 rounded-lg text-sm"
          >
            <Tag size={12} /> {filterTag} <X size={12} />
          </button>
        )}
        {compareArr.length === 2 && (
          <button
            onClick={() => setShowCompare(true)}
            className="flex items-center gap-1.5 px-3 py-2 bg-print-600 hover:bg-print-700 rounded-lg text-sm text-white transition-colors"
          >
            <GitCompare size={14} /> Compare
          </button>
        )}
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading...</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <Archive size={40} className="mx-auto mb-3 opacity-30" />
          <p>No archived prints yet</p>
          <p className="text-xs mt-1">Prints are automatically archived when they complete</p>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border border-farm-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-farm-900 text-farm-400 text-xs">
                  <th className="py-2 px-2 w-8">
                    <span className="sr-only">Compare</span>
                  </th>
                  <th className="py-2 px-3 text-left w-10"></th>
                  <th className="py-2 px-3 text-left">Name</th>
                  <th className="py-2 px-3 text-left hidden md:table-cell">Printer</th>
                  <th className="py-2 px-3 text-left">Status</th>
                  <th className="py-2 px-3 text-left hidden lg:table-cell">Duration</th>
                  <th className="py-2 px-3 text-left hidden lg:table-cell">Filament</th>
                  <th className="py-2 px-3 text-left">Date</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr
                    key={item.id}
                    onClick={() => setSelected(item)}
                    className="border-t border-farm-800 hover:bg-farm-800/50 cursor-pointer transition-colors"
                  >
                    <td className="py-2 px-2" onClick={e => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={compareIds.has(item.id)}
                        onChange={() => toggleCompare(item.id)}
                        disabled={!compareIds.has(item.id) && compareIds.size >= 2}
                        className="accent-print-500"
                      />
                    </td>
                    <td className="py-2 px-3">
                      {item.thumbnail_b64 ? (
                        <img src={`data:image/png;base64,${item.thumbnail_b64}`} alt="" className="w-8 h-8 rounded object-cover" />
                      ) : (
                        <div className="w-8 h-8 rounded bg-farm-800 flex items-center justify-center">
                          <Archive size={12} className="text-farm-600" />
                        </div>
                      )}
                    </td>
                    <td className="py-2 px-3">
                      <div className="font-medium truncate max-w-[200px]">{item.print_name}</div>
                      {item.tags && item.tags.length > 0 && (
                        <TagChips tags={item.tags} onTagClick={t => { setFilterTag(t); setPage(1) }} />
                      )}
                    </td>
                    <td className="py-2 px-3 text-farm-400 hidden md:table-cell">{item.printer_display}</td>
                    <td className="py-2 px-3">
                      <span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[item.status] || 'bg-farm-800 text-farm-400'}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-farm-400 hidden lg:table-cell">
                      <span className="flex items-center gap-1"><Clock size={12} /> {formatDuration(item.actual_duration_seconds)}</span>
                    </td>
                    <td className="py-2 px-3 text-farm-400 hidden lg:table-cell">
                      {item.filament_used_grams ? `${item.filament_used_grams.toFixed(1)}g` : '--'}
                    </td>
                    <td className="py-2 px-3 text-farm-400 text-xs">{formatDate(item.completed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <span className="text-xs text-farm-500">{total} results</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="p-1.5 rounded-lg bg-farm-800 text-farm-400 hover:bg-farm-700 disabled:opacity-30"
                >
                  <ChevronLeft size={16} />
                </button>
                <span className="text-sm text-farm-400">{page} / {totalPages}</span>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="p-1.5 rounded-lg bg-farm-800 text-farm-400 hover:bg-farm-700 disabled:opacity-30"
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Detail modal */}
      {selected && (
        <ArchiveDetail
          archive={selected}
          onClose={() => setSelected(null)}
          onDelete={(id) => deleteMutation.mutate(id)}
          onNotesUpdate={(id, notes) => archives.update(id, { notes })}
          onTagsUpdate={(id, tags) => {
            archives.updateTags(id, tags)
            queryClient.invalidateQueries({ queryKey: ['archives'] })
          }}
          printerList={printerList}
        />
      )}

      {/* Compare modal */}
      {showCompare && compareArr.length === 2 && (
        <CompareModal
          a={items.find(i => i.id === compareArr[0]) || { id: compareArr[0] }}
          b={items.find(i => i.id === compareArr[1]) || { id: compareArr[1] }}
          onClose={() => setShowCompare(false)}
        />
      )}
    </div>
  )
}
