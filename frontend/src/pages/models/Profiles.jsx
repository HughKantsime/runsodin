import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { profilesApi, printers as printersApi } from '../../api'
import { SlidersHorizontal, Upload, Plus, Download, Play, Trash2, Pencil, Loader2, Search, X } from 'lucide-react'
import ConfirmModal from '../../components/shared/ConfirmModal'

const SLICER_TABS = [
  { key: '', label: 'All' },
  { key: 'klipper', label: 'Klipper' },
  { key: 'orca', label: 'OrcaSlicer' },
  { key: 'bambu_studio', label: 'Bambu Studio' },
  { key: 'prusa', label: 'PrusaSlicer' },
]

const CATEGORY_CHIPS = [
  { key: '', label: 'All' },
  { key: 'printer', label: 'Printer' },
  { key: 'filament', label: 'Filament' },
  { key: 'process', label: 'Process' },
  { key: 'temperature', label: 'Temperature' },
  { key: 'speed', label: 'Speed' },
  { key: 'macro_set', label: 'Macros' },
]

const SLICER_COLORS = {
  klipper: 'bg-green-500/20 text-green-400',
  orca: 'bg-blue-500/20 text-blue-400',
  bambu_studio: 'bg-purple-500/20 text-purple-400',
  prusa: 'bg-orange-500/20 text-orange-400',
  generic: 'bg-farm-500/20 text-farm-400',
}

export default function Profiles({ filterPrinterId, filterSlicer } = {}) {
  const queryClient = useQueryClient()
  const fileInputRef = useRef(null)
  const [slicer, setSlicer] = useState(filterSlicer || '')
  const [category, setCategory] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  const [applyProfile, setApplyProfile] = useState(null)
  const [applyPrinterId, setApplyPrinterId] = useState('')
  const [importing, setImporting] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['profiles', slicer, category, search, page, filterPrinterId],
    queryFn: () => profilesApi.list({ slicer, category, search: search || undefined, page, printer_id: filterPrinterId }),
  })

  const { data: printersList } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printersApi.list(),
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => profilesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] })
      toast.success('Profile deleted')
    },
    onError: (e) => toast.error(e.message || 'Delete failed'),
  })

  const applyMutation = useMutation({
    mutationFn: ({ profileId, printerId }) => profilesApi.apply(profileId, printerId),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] })
      toast.success(`Profile applied — ${res.commands_sent} command(s) sent`)
      setApplyProfile(null)
    },
    onError: (e) => toast.error(e.message || 'Apply failed'),
  })

  const handleImport = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setImporting(true)
    try {
      const res = await profilesApi.import(file)
      queryClient.invalidateQueries({ queryKey: ['profiles'] })
      toast.success(`Imported ${res.imported} profile(s)`)
    } catch (err) {
      toast.error(err.message || 'Import failed')
    } finally {
      setImporting(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const items = data?.profiles || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / 50)
  const klipperPrinters = (printersList || []).filter(p => p.api_type === 'moonraker' && p.is_active)

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <SlidersHorizontal className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Profiles</h1>
            <p className="text-farm-500 text-sm mt-1">Slicer and printer configuration profiles</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <input ref={fileInputRef} type="file" accept=".json,.ini,.3mf" className="hidden" onChange={handleImport} />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            className="flex items-center gap-1.5 px-3 py-2 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm transition-colors"
          >
            {importing ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
            Import
          </button>
        </div>
      </div>

      {/* Slicer tabs */}
      <div className="flex flex-wrap gap-1 mb-4">
        {SLICER_TABS.map(t => (
          <button
            key={t.key}
            onClick={() => { setSlicer(t.key); setPage(1) }}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              slicer === t.key ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-300 hover:bg-farm-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Category chips + search */}
      <div className="flex flex-wrap items-center gap-2 mb-6">
        {CATEGORY_CHIPS.map(c => (
          <button
            key={c.key}
            onClick={() => { setCategory(c.key); setPage(1) }}
            className={`px-2.5 py-1 rounded-full text-[11px] font-medium transition-colors ${
              category === c.key ? 'bg-farm-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'
            }`}
          >
            {c.label}
          </button>
        ))}
        <div className="flex items-center gap-1 ml-auto bg-farm-800 rounded-lg px-3 py-1.5 border border-farm-700">
          <Search size={12} className="text-farm-500" />
          <input
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1) }}
            placeholder="Search profiles..."
            className="bg-transparent text-sm text-farm-200 outline-none w-40"
          />
          {search && <button onClick={() => setSearch('')}><X size={12} className="text-farm-500" /></button>}
        </div>
      </div>

      {/* Profile list */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-farm-400">
          <Loader2 className="animate-spin mr-2" size={20} /> Loading...
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-20 text-farm-500">
          <SlidersHorizontal size={48} className="mx-auto mb-4 opacity-50" />
          <p className="text-lg">No profiles yet</p>
          <p className="text-sm mt-1">Import a .json, .ini, or .3mf file to get started</p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map(p => (
            <div key={p.id} className="bg-farm-800 rounded-lg border border-farm-700 p-4 flex items-center gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-farm-200 truncate">{p.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${SLICER_COLORS[p.slicer] || SLICER_COLORS.generic}`}>
                    {p.slicer}
                  </span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-farm-700 text-farm-300">
                    {p.category}
                  </span>
                  {p.filament_type && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-farm-700 text-farm-400">
                      {p.filament_type}
                    </span>
                  )}
                </div>
                {p.description && <p className="text-xs text-farm-500 truncate">{p.description}</p>}
                <div className="flex items-center gap-3 mt-1 text-[10px] text-farm-500">
                  {p.tags && <span>{p.tags}</span>}
                  {p.last_applied_at && <span>Applied {new Date(p.last_applied_at).toLocaleDateString()}</span>}
                </div>
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                {p.slicer === 'klipper' && (
                  <button
                    onClick={() => { setApplyProfile(p); setApplyPrinterId('') }}
                    className="flex items-center gap-1 px-2 py-1 bg-green-600/20 hover:bg-green-600/40 text-green-400 rounded text-xs transition-colors"
                    title="Apply to printer"
                  >
                    <Play size={10} /> Apply
                  </button>
                )}
                <a
                  href={profilesApi.exportUrl(p.id)}
                  className="flex items-center gap-1 px-2 py-1 bg-farm-700 hover:bg-farm-600 rounded text-xs transition-colors text-farm-300"
                  title="Download"
                >
                  <Download size={10} />
                </a>
                <button
                  onClick={() => setConfirmDeleteId(p.id)}
                  className="flex items-center gap-1 px-2 py-1 bg-farm-700 hover:bg-red-600/30 text-farm-400 hover:text-red-400 rounded text-xs transition-colors"
                  title="Delete"
                >
                  <Trash2 size={10} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-4 mt-6">
          <button
            onClick={() => setPage(Math.max(1, page - 1))} disabled={page <= 1}
            className="px-3 py-1.5 rounded-lg bg-farm-800 text-sm text-farm-300 hover:bg-farm-700 disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-sm text-farm-400">Page {page} of {totalPages}</span>
          <button
            onClick={() => setPage(page + 1)} disabled={page >= totalPages}
            className="px-3 py-1.5 rounded-lg bg-farm-800 text-sm text-farm-300 hover:bg-farm-700 disabled:opacity-50"
          >
            Next
          </button>
        </div>
      )}

      {/* Apply modal */}
      {applyProfile && (
        <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" onClick={() => setApplyProfile(null)}>
          <div className="bg-farm-900 rounded-xl border border-farm-700 p-6 max-w-md w-full" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-medium mb-4">Apply "{applyProfile.name}"</h3>
            <p className="text-sm text-farm-400 mb-4">Select a Klipper printer. This sends GCode immediately.</p>
            <select
              value={applyPrinterId}
              onChange={e => setApplyPrinterId(e.target.value)}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm mb-4"
            >
              <option value="">Select printer...</option>
              {klipperPrinters.map(p => (
                <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
              ))}
            </select>
            <div className="flex justify-end gap-2">
              <button onClick={() => setApplyProfile(null)} className="px-4 py-2 text-sm text-farm-400 hover:text-white">Cancel</button>
              <button
                onClick={() => applyMutation.mutate({ profileId: applyProfile.id, printerId: parseInt(applyPrinterId) })}
                disabled={!applyPrinterId || applyMutation.isPending}
                className="px-4 py-2 bg-green-600 hover:bg-green-500 rounded-lg text-sm font-medium disabled:opacity-50"
              >
                {applyMutation.isPending ? 'Applying...' : 'Apply'}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmModal
        open={!!confirmDeleteId}
        title="Delete Profile"
        message="Are you sure? This cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
        onConfirm={() => { deleteMutation.mutate(confirmDeleteId); setConfirmDeleteId(null) }}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </div>
  )
}
