import type { ReactNode } from 'react'
import type { Archive as ArchiveDto } from '../../types'
import { useState, useCallback, useEffect, useRef, lazy, Suspense } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { archives, printers as printersApi } from '../../api'
import { canDo } from '../../permissions'
const ModelViewer = lazy(() => import('../../components/models/ModelViewer'))
import { Archive, X, Trash2, Clock, ChevronLeft, ChevronRight, GitCompare, Tag, RotateCcw, Box } from 'lucide-react'
import toast from 'react-hot-toast'
import { formatDurationSecs as formatDuration, formatDate } from '../../utils/shared'
import { PageHeader, SearchInput, StatusBadge as SharedStatusBadge, Button, EmptyState, Modal } from '../../components/ui'

const STATUS_BADGES = {
  completed: 'bg-green-600/20 text-green-400',
  failed: 'bg-red-600/20 text-red-400',
  cancelled: 'bg-yellow-600/20 text-yellow-400',
}

function TagChips({ tags, onTagClick }: { tags: string[]; onTagClick?: (tag: string) => void }) {
  if (!tags || tags.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map(t => (
        <button
          key={t}
          onClick={e => { e.stopPropagation(); onTagClick?.(t) }}
          className="px-1.5 py-0.5 bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded text-xs hover:bg-[var(--brand-primary)]/30 transition-colors"
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
      <label className="block text-xs text-[var(--brand-text-muted)] mb-1">Tags</label>
      <div className="flex flex-wrap gap-1 mb-2">
        {tags.map(t => (
          <span key={t} className="flex items-center gap-1 px-2 py-0.5 bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded text-xs">
            {t}
            <button aria-label={`Remove tag ${t}`} onClick={() => onChange(tags.filter(x => x !== t))} className="hover:text-red-400">
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
          className="flex-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-2 py-1 text-xs"
        />
        <button onClick={addTag} className="px-2 py-1 bg-[var(--brand-primary)] hover:opacity-90 rounded text-xs">Add</button>
      </div>
    </div>
  )
}

function CompareModal({ a, b, onClose }) {
  const { data, isLoading } = useQuery({
    queryKey: ['archive-compare', a.id, b.id],
    queryFn: () => archives.compare(a.id, b.id),
  })

  const field = <K extends keyof ArchiveDto>(key: K, label: string, format: (value: ArchiveDto[K]) => ReactNode) => ({
    key, label, format: (archive: ArchiveDto) => format(archive[key]),
  })
  const fields = [
    field('printer_display', 'Printer', v => v ?? '--'),
    field('status', 'Status', v => v ?? '--'),
    field('actual_duration_seconds', 'Duration', formatDuration),
    field('filament_used_grams', 'Filament (g)', v => v != null ? `${v.toFixed(1)}g` : '--'),
    field('cost_estimate', 'Cost', v => v != null ? `$${v.toFixed(2)}` : '--'),
    field('started_at', 'Started', formatDate),
    field('completed_at', 'Completed', formatDate),
  ]

  return (
    <Modal isOpen={true} onClose={onClose} title="Compare Archives" size="xl" mobileSheet={false}>
      {isLoading ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)]">Loading...</div>
      ) : data ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--brand-text-muted)] text-xs border-b border-[var(--brand-card-border)]">
                <th className="py-2 px-3 text-left">Field</th>
                <th className="py-2 px-3 text-left truncate max-w-[200px]">{data.a.print_name}</th>
                <th className="py-2 px-3 text-left truncate max-w-[200px]">{data.b.print_name}</th>
              </tr>
            </thead>
            <tbody>
              {fields.map(f => {
                const isDiff = data.diff && f.key in data.diff
                return (
                  <tr key={f.key} className={`border-t border-[var(--brand-card-border)] ${isDiff ? 'bg-amber-900/20' : ''}`}>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)]">{f.label}</td>
                    <td className="py-2 px-3">{f.format(data.a)}</td>
                    <td className="py-2 px-3">{f.format(data.b)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </Modal>
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
  const [show3DViewer, setShow3DViewer] = useState(false)

  const plateCount = archive.plate_count || 1
  const [applyMapping, setApplyMapping] = useState(true)
  const targetPrinter = (printerList || []).find(p => String(p.id) === String(reprintPrinterId))
  const { data: amsPreview, isLoading: amsLoading, isError: amsError } = useQuery({
    queryKey: ['archive-ams-preview', archive.id, reprintPrinterId],
    queryFn: () => archives.amsPreview(archive.id, parseInt(reprintPrinterId)),
    enabled: showReprint && !!reprintPrinterId,
  })
  const required = amsPreview?.required || []
  const suggested = amsPreview?.suggested_mapping || []
  // Bambu dispatch accepts a mapping only when it covers every source filament.
  const mappingComplete = required.length > 0 && suggested.length === required.length
  const canApplyMapping = mappingComplete && targetPrinter?.api_type === 'bambu'

  const handleReprint = async () => {
    if (!reprintPrinterId) return toast.error('Select a printer')
    setReprinting(true)
    try {
      const result = await archives.reprint(archive.id, {
        printer_id: parseInt(reprintPrinterId),
        plate_index: reprintPlate,
        ...(canApplyMapping && applyMapping
          ? { ams_mapping: suggested.map(m => ({ source_slot: m.source_slot, target_slot: m.target_slot })) }
          : {}),
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
    <>
      <Modal isOpen={true} onClose={onClose} title={archive.print_name} size="lg">
        {/* Thumbnail */}
        {archive.thumbnail_b64 && (
          <div className="mb-4 rounded-md overflow-hidden bg-black flex items-center justify-center" style={{ maxHeight: '200px' }}>
            <img src={`data:image/png;base64,${archive.thumbnail_b64}`} alt="Thumbnail" className="max-h-[200px] object-contain" />
          </div>
        )}

        {/* 3D Viewer button */}
        {archive.print_file_id && (
          <button
            onClick={() => setShow3DViewer(true)}
            className="w-full mb-4 py-2 rounded-md text-sm text-amber-400 bg-amber-600/10 hover:bg-amber-600/20 transition-colors flex items-center justify-center gap-2"
          >
            <Box size={14} /> View 3D Model
          </button>
        )}

        {/* Details grid */}
        <div className="grid grid-cols-2 gap-3 text-sm mb-4">
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Printer</span>
            <p>{archive.printer_display}</p>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Status</span>
            <p><span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[archive.status] || 'bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]'}`}>{archive.status}</span></p>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Started</span>
            <p className="text-xs">{formatDate(archive.started_at)}</p>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Completed</span>
            <p className="text-xs">{formatDate(archive.completed_at)}</p>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Duration</span>
            <p>{formatDuration(archive.actual_duration_seconds)}</p>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)] text-xs">Filament</span>
            <p>{archive.filament_used_grams != null ? `${archive.filament_used_grams.toFixed(1)}g` : '--'}</p>
          </div>
        </div>

        {/* Tags */}
        {canDo('printers.edit') ? (
          <div className="mb-4">
            <TagEditor tags={archiveTags} onChange={saveTags} />
          </div>
        ) : archiveTags.length > 0 ? (
          <div className="mb-4">
            <label className="block text-xs text-[var(--brand-text-muted)] mb-1">Tags</label>
            <TagChips tags={archiveTags} />
          </div>
        ) : null}

        {/* Notes */}
        {canDo('printers.edit') && (
          <div className="mb-4">
            <label className="block text-xs text-[var(--brand-text-muted)] mb-1">Notes</label>
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              rows={3}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm resize-none"
              placeholder="Add notes..."
            />
            <button
              onClick={saveNotes}
              disabled={saving}
              className="mt-1 px-3 py-1 bg-[var(--brand-primary)] hover:opacity-90 rounded text-xs disabled:opacity-50"
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
                className="w-full py-2 rounded-md text-sm text-[var(--brand-primary)] bg-[var(--brand-primary)]/10 hover:bg-[var(--brand-primary)]/20 transition-colors flex items-center justify-center gap-2"
              >
                <RotateCcw size={14} /> Reprint
              </button>
            ) : (
              <div className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md p-3 space-y-3">
                <label className="block text-xs text-[var(--brand-text-muted)]">Target Printer</label>
                <select
                  value={reprintPrinterId}
                  onChange={e => setReprintPrinterId(e.target.value)}
                  className="w-full bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded px-2 py-1.5 text-sm"
                >
                  <option value="">Select printer...</option>
                  {(printerList || []).filter(p => p.is_active).map(p => (
                    <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
                  ))}
                </select>
                {reprintPrinterId && (
                  <div data-testid="reprint-ams-preview" className="space-y-1">
                    <span className="block text-xs text-[var(--brand-text-muted)]">AMS filament mapping</span>
                    {amsLoading ? (
                      <p className="text-xs text-[var(--brand-text-muted)]">Loading filament mapping...</p>
                    ) : amsError ? (
                      <p role="alert" className="text-xs text-red-400">Filament mapping preview unavailable</p>
                    ) : required.length === 0 ? (
                      <p className="text-xs text-[var(--brand-text-muted)]">The original file lists no filaments.</p>
                    ) : (
                      <ul className="space-y-1">
                        {required.map(req => {
                          const match = suggested.find(m => m.source_slot === req.slot)
                          const slot = match && (amsPreview?.available || []).find(a => a.slot === match.target_slot)
                          return (
                            <li key={req.slot} className="flex items-center gap-2 text-xs">
                              <span className="inline-block w-3 h-3 rounded-full border border-[var(--brand-card-border)]" style={{ background: req.color || 'transparent' }} />
                              <span>Filament {req.slot + 1}: {req.material || 'Unknown'}</span>
                              <span className="text-[var(--brand-text-muted)]">&rarr;</span>
                              {slot ? (
                                <span>Slot {slot.slot} ({[slot.color, slot.material].filter(Boolean).join(' ')}){match.confidence === 'material_only' ? ' - material only' : ''}</span>
                              ) : (
                                <span className="text-amber-400">No matching slot</span>
                              )}
                            </li>
                          )
                        })}
                      </ul>
                    )}
                    {canApplyMapping && (
                      <label className="flex items-center gap-2 text-xs">
                        <input type="checkbox" checked={applyMapping} onChange={e => setApplyMapping(e.target.checked)} />
                        Use this mapping
                      </label>
                    )}
                  </div>
                )}
                {plateCount > 1 && (
                  <>
                    <label className="block text-xs text-[var(--brand-text-muted)]">Plate</label>
                    <div className="flex flex-wrap gap-2">
                      {Array.from({ length: plateCount }, (_, i) => (
                        <button
                          key={i}
                          onClick={() => setReprintPlate(i)}
                          className={`px-3 py-1 rounded text-xs border transition-colors ${
                            reprintPlate === i
                              ? 'border-[var(--brand-primary)] bg-[var(--brand-primary)]/10 text-[var(--brand-primary)]'
                              : 'border-[var(--brand-card-border)] text-[var(--brand-text-muted)] hover:border-[var(--brand-text-muted)]'
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
                    className="flex-1 py-1.5 rounded text-xs bg-[var(--brand-card-border)] hover:opacity-80 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleReprint}
                    disabled={reprinting || !reprintPrinterId}
                    className="flex-1 py-1.5 rounded text-xs bg-[var(--brand-primary)] hover:opacity-90 transition-colors disabled:opacity-50"
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
            className="w-full py-2 rounded-md text-sm text-red-400 bg-red-600/10 hover:bg-red-600/20 transition-colors"
          >
            Delete Archive
          </button>
        )}
      </Modal>

      {/* 3D Model Viewer overlay */}
      {show3DViewer && archive.print_file_id && (
        <Suspense fallback={<div className="flex items-center justify-center p-8 text-[var(--brand-text-muted)]">Loading 3D viewer...</div>}>
          <ModelViewer
            fileId={archive.print_file_id}
            modelName={archive.print_name}
            onClose={() => setShow3DViewer(false)}
          />
        </Suspense>
      )}
    </>
  )
}

export default function ArchivesPage() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [page, _setPage] = useState(() => {
    const p = parseInt(searchParams.get('page'))
    return p > 0 ? p : 1
  })
  const [search, _setSearch] = useState(() => searchParams.get('q') || '')
  const [searchInput, setSearchInput] = useState(() => searchParams.get('q') || '')
  const [filterPrinter, _setFilterPrinter] = useState(() => searchParams.get('printer') || '')
  const [filterStatus, _setFilterStatus] = useState(() => searchParams.get('status') || '')
  const [filterTag, _setFilterTag] = useState(() => searchParams.get('tag') || '')
  const [selected, setSelected] = useState(null)
  const [compareIds, setCompareIds] = useState(new Set<number>())
  const [showCompare, setShowCompare] = useState(false)

  // Several filter setters run in one handler (e.g. tag click = tag + page 1).
  // react-router's functional setSearchParams hands each call the params of
  // the last render, so the second write would drop the first: accumulate in
  // a ref and write the composed result.
  const paramsRef = useRef(searchParams)
  useEffect(() => { paramsRef.current = searchParams }, [searchParams])
  const updateSearchParams = useCallback((updates: Record<string, string>) => {
    const next = new URLSearchParams(paramsRef.current)
    Object.entries(updates).forEach(([key, value]) => {
      if (value && value !== '' && !(key === 'page' && String(value) === '1')) {
        next.set(key, String(value))
      } else {
        next.delete(key)
      }
    })
    paramsRef.current = next
    setSearchParams(next, { replace: true })
  }, [setSearchParams])

  const pageRef = useRef(page)
  useEffect(() => { pageRef.current = page }, [page])
  const setPage = useCallback((valueOrFn) => {
    const newVal = typeof valueOrFn === 'function' ? valueOrFn(pageRef.current) : valueOrFn
    pageRef.current = newVal
    _setPage(newVal)
    updateSearchParams({ page: newVal })
  }, [updateSearchParams])

  const setSearch = useCallback((value) => {
    _setSearch(value)
    updateSearchParams({ q: value })
  }, [updateSearchParams])

  const setFilterPrinter = useCallback((value) => {
    _setFilterPrinter(value)
    updateSearchParams({ printer: value })
  }, [updateSearchParams])

  const setFilterStatus = useCallback((value) => {
    _setFilterStatus(value)
    updateSearchParams({ status: value })
  }, [updateSearchParams])

  const setFilterTag = useCallback((value) => {
    _setFilterTag(value)
    updateSearchParams({ tag: value })
  }, [updateSearchParams])

  const { data: printerList } = useQuery({
    queryKey: ['printers-list'],
    queryFn: () => printersApi.list(),
  })

  const { data, isLoading, isError, refetch } = useQuery({
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
    mutationFn: (id: Parameters<typeof archives.delete>[0]) => archives.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] })
      toast.success('Archive deleted')
    },
    onError: (err: any) => toast.error('Delete archive failed: ' + (err?.message || 'Unknown error')),
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
      <PageHeader icon={Archive} title="Print Archive" subtitle={`${total} archived prints`} />

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <SearchInput
          value={searchInput}
          onChange={e => setSearchInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { setSearch(searchInput); setPage(1) } }}
          placeholder="Search prints..."
          className="flex-1 min-w-[200px] max-w-xs"
        />
        <select aria-label="Filter by printer"
          value={filterPrinter}
          onChange={e => { setFilterPrinter(e.target.value); setPage(1) }}
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-2 text-sm"
        >
          <option value="">All Printers</option>
          {(printerList || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>
        <select aria-label="Filter by status"
          value={filterStatus}
          onChange={e => { setFilterStatus(e.target.value); setPage(1) }}
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        {filterTag && (
          <button
            onClick={() => { setFilterTag(''); setPage(1) }}
            className="flex items-center gap-1 px-2 py-2 bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded-md text-sm"
          >
            <Tag size={12} /> {filterTag} <X size={12} />
          </button>
        )}
        {compareArr.length === 2 && (
          <Button icon={GitCompare} onClick={() => setShowCompare(true)}>
            Compare
          </Button>
        )}
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="text-center py-12 text-[var(--brand-text-muted)]">Loading...</div>
      ) : isError ? (
        <div role="alert" className="py-8 text-center">
          <p>Archive history unavailable</p>
          <Button onClick={() => refetch()} className="mt-3">Retry</Button>
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={Archive}
          title="No archived prints yet"
          description="Prints are automatically archived when they complete"
        />
      ) : (
        <>
          <div className="overflow-x-auto rounded-md border border-[var(--brand-card-border)]">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-[var(--brand-card-bg)] text-[var(--brand-text-muted)] text-xs">
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
                    className="border-t border-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)]/50 cursor-pointer transition-colors"
                  >
                    <td className="py-2 px-2" onClick={e => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={compareIds.has(item.id)}
                        onChange={() => toggleCompare(item.id)}
                        disabled={!compareIds.has(item.id) && compareIds.size >= 2}
                        className="accent-[var(--brand-primary)]"
                      />
                    </td>
                    <td className="py-2 px-3">
                      {item.thumbnail_b64 ? (
                        <img src={`data:image/png;base64,${item.thumbnail_b64}`} alt="" className="w-8 h-8 rounded object-cover" />
                      ) : (
                        <div className="w-8 h-8 rounded bg-[var(--brand-input-bg)] flex items-center justify-center">
                          <Archive size={12} className="text-[var(--brand-text-muted)]" />
                        </div>
                      )}
                    </td>
                    <td className="py-2 px-3">
                      <div className="font-medium truncate max-w-[200px]">{item.print_name}</div>
                      {item.tags && item.tags.length > 0 && (
                        <TagChips tags={item.tags} onTagClick={t => { setFilterTag(t); setPage(1) }} />
                      )}
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden md:table-cell">{item.printer_display}</td>
                    <td className="py-2 px-3">
                      <SharedStatusBadge status={item.status} size="sm" />
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden lg:table-cell">
                      <span className="flex items-center gap-1"><Clock size={12} /> {formatDuration(item.actual_duration_seconds)}</span>
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden lg:table-cell">
                      {item.filament_used_grams != null ? `${item.filament_used_grams.toFixed(1)}g` : '--'}
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] text-xs">{formatDate(item.completed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <span className="text-xs text-[var(--brand-text-muted)]">{total} results</span>
              <div className="flex items-center gap-2">
                <Button aria-label="Previous page"
                  variant="secondary"
                  size="icon"
                  icon={ChevronLeft}
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                />
                <span className="text-sm text-[var(--brand-text-muted)]">{page} / {totalPages}</span>
                <Button aria-label="Next page"
                  variant="secondary"
                  size="icon"
                  icon={ChevronRight}
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                />
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
