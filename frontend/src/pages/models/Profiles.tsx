import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { profilesApi, printers as printersApi } from '../../api'
import { SlidersHorizontal, Upload, Plus, Download, Play, Trash2, Pencil, Loader2, Search, X } from 'lucide-react'
import ConfirmModal from '../../components/shared/ConfirmModal'
import ProfileHistory from './ProfileHistory'
import { canDo, getCurrentUser } from '../../permissions'

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
  generic: 'bg-[var(--brand-muted)]/20 text-[var(--brand-muted)]',
}

export default function Profiles({ filterPrinterId, filterSlicer } = {}) {
  const queryClient = useQueryClient()
  const fileInputRef = useRef(null)
  const [slicer, setSlicer] = useState(filterSlicer || '')
  const [category, setCategory] = useState('')
  const [printerModel, setPrinterModel] = useState('')
  const [material, setMaterial] = useState('')
  const [tagProfile, setTagProfile] = useState(null)
  const [tags, setTags] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  const [applyProfile, setApplyProfile] = useState(null)
  const [applyPrinterId, setApplyPrinterId] = useState('')
  const [importing, setImporting] = useState(false)
  const [historyProfile, setHistoryProfile] = useState(null)

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['profiles', slicer, category, search, page, filterPrinterId, printerModel, material],
    queryFn: () => profilesApi.list({ slicer, category, search: search || undefined, page, printer_id: filterPrinterId, ...(printerModel ? { printer_model: printerModel } : {}), ...(material ? { filament_type: material } : {}) }),
  })

  const { data: printersList } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printersApi.list(),
  })

  const tagsMutation = useMutation({
    mutationFn: ({ id, tags }) => profilesApi.update(id, { tags }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] })
      setTagProfile(null)
      toast.success('Profile tags saved')
    },
    onError: (e) => toast.error(e.message || 'Save failed'),
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
      if (res.applied === true) {
        toast.success(`Profile applied: ${res.commands_sent} command(s) sent`)
        setApplyProfile(null)
      } else {
        toast.error(res.outcome === 'partial'
          ? 'Some commands failed. Check the printer before trying again.'
          : 'Profile was not applied. Printer commands failed.')
      }
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
    } catch (err: any) {
      toast.error(err.message || 'Import failed')
    } finally {
      setImporting(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const items = data?.profiles || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / 50)
  const currentUser = getCurrentUser()
  const operator = currentUser && ['operator', 'admin'].includes(currentUser.role)
  const canManageProfiles = operator && canDo('models.edit')
  const canApplyProfiles = operator && canDo('printers.edit')
  const klipperPrinters = (printersList || []).filter(p => p.api_type === 'moonraker' && p.is_active && currentUser && (
    (currentUser.role === 'admin' && !currentUser.group_id) || p.org_id === null ||
    (currentUser.group_id != null && p.org_id === currentUser.group_id)
  ))

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <SlidersHorizontal className="text-[var(--brand-primary)]" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Profiles</h1>
            <p className="text-[var(--brand-muted)] text-sm mt-1">Slicer and printer configuration profiles</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {canManageProfiles && <input ref={fileInputRef} type="file" accept=".json,.ini,.cfg,.3mf" className="hidden" onChange={handleImport} />}
          {canManageProfiles && <button
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            className="flex items-center gap-1.5 px-3 py-2 bg-[var(--brand-card-border)] hover:bg-[var(--brand-card-border)] rounded-md text-sm transition-colors"
          >
            {importing ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
            Import
          </button>}
        </div>
      </div>

      {/* Slicer tabs */}
      <div className="flex flex-wrap gap-1 mb-4">
        {SLICER_TABS.map(t => (
          <button
            key={t.key}
            onClick={() => { setSlicer(t.key); setPage(1) }}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              slicer === t.key ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)]'
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
              category === c.key ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--brand-input-bg)] text-[var(--brand-muted)] hover:bg-[var(--brand-card-border)]'
            }`}
          >
            {c.label}
          </button>
        ))}
        <div className="flex items-center gap-1 ml-auto bg-[var(--brand-input-bg)] rounded-md px-3 py-1.5 border border-[var(--brand-card-border)]">
          <Search size={12} className="text-[var(--brand-muted)]" />
          <input
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1) }}
            placeholder="Search profiles..."
            className="bg-transparent text-sm text-[var(--brand-text)] outline-hidden w-40"
          />
          {search && <button onClick={() => setSearch('')}><X size={12} className="text-[var(--brand-muted)]" /></button>}
        </div>
      </div>

      <div className="flex flex-wrap gap-3 mb-6">
        <label className="text-sm">Printer model
          <input role="combobox" aria-expanded="false" aria-autocomplete="list" list="profile-printer-models" value={printerModel}
            onChange={e => { setPrinterModel(e.target.value); setPage(1) }} placeholder="All printer models"
            className="ml-2 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2" />
          <datalist id="profile-printer-models">{[...new Set((printersList || []).map(p => p.model).filter(Boolean))].map(model => <option key={model} value={model} />)}</datalist>
        </label>
        <label className="text-sm">Material
          <input role="combobox" aria-expanded="false" aria-autocomplete="list" list="profile-materials" value={material}
            onChange={e => { setMaterial(e.target.value); setPage(1) }} placeholder="All materials"
            className="ml-2 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2" />
          <datalist id="profile-materials">{['PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PA', 'PC'].map(value => <option key={value} value={value} />)}</datalist>
        </label>
      </div>

      {/* Profile list */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-[var(--brand-muted)]">
          <Loader2 className="animate-spin mr-2" size={20} /> Loading...
        </div>
      ) : isError ? (
        <div role="alert" className="text-center py-20 text-[var(--brand-muted)]">
          <p>Unable to load profiles</p>
          <button onClick={() => refetch()} className="mt-3 px-4 py-2 rounded-md bg-[var(--brand-input-bg)]">Retry</button>
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-20 text-[var(--brand-muted)]">
          <SlidersHorizontal size={48} className="mx-auto mb-4 opacity-50" />
          <p className="text-lg">No profiles yet</p>
          <p className="text-sm mt-1">Import a .json, .ini, .cfg, or .3mf file to get started</p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map(p => (
            <div key={p.id} className="bg-[var(--brand-input-bg)] rounded-md border border-[var(--brand-card-border)] p-4 flex flex-col sm:flex-row sm:items-center gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-[var(--brand-text)] truncate">{p.name}</span>
                  <span className={`text-xs px-1.5 py-0.5 rounded-full ${SLICER_COLORS[p.slicer] || SLICER_COLORS.generic}`}>
                    {p.slicer}
                  </span>
                  <span className="text-xs px-1.5 py-0.5 rounded-full bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)]">
                    {p.category}
                  </span>
                  {p.filament_type && (
                    <span className="text-xs px-1.5 py-0.5 rounded-full bg-[var(--brand-card-border)] text-[var(--brand-muted)]">
                      {p.filament_type}
                    </span>
                  )}
                </div>
                {p.description && <p className="text-xs text-[var(--brand-muted)] truncate">{p.description}</p>}
                <div className="flex items-center gap-3 mt-1 text-xs text-[var(--brand-muted)]">
                  {p.tags && <span>{p.tags}</span>}
                  {p.last_applied_at && <span>Applied {new Date(p.last_applied_at).toLocaleDateString()}</span>}
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-1.5 shrink-0">
                <button onClick={() => setHistoryProfile(p)} className="px-2 py-1 text-xs rounded-md border border-[var(--brand-card-border)]" title="Profile history">History</button>
                {canApplyProfiles && p.slicer === 'klipper' && p.file_format !== 'cfg' && (
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
                  className="flex items-center gap-1 px-2 py-1 bg-[var(--brand-card-border)] hover:bg-[var(--brand-card-border)] rounded text-xs transition-colors text-[var(--brand-text-secondary)]"
                  title="Download"
                >
                  <Download size={10} />
                </a>
                {canManageProfiles && (currentUser.role === 'admin' || p.created_by === currentUser.id) && <button
                  onClick={() => { setTagProfile(p); setTags(p.tags || '') }} className="px-2 py-1 text-xs rounded-md border border-[var(--brand-card-border)]">Edit tags<span className="sr-only"> for {p.name}</span></button>}
                {canManageProfiles && (currentUser.role === 'admin' || p.created_by === currentUser.id) && <button
                  onClick={() => setConfirmDeleteId(p.id)}
                  className="flex items-center gap-1 px-2 py-1 bg-[var(--brand-card-border)] hover:bg-red-600/30 text-[var(--brand-muted)] hover:text-red-400 rounded text-xs transition-colors"
                  title="Delete"
                >
                  <Trash2 size={10} />
                </button>}
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
            className="px-3 py-1.5 rounded-md bg-[var(--brand-input-bg)] text-sm text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)] disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-sm text-[var(--brand-muted)]">Page {page} of {totalPages}</span>
          <button
            onClick={() => setPage(page + 1)} disabled={page >= totalPages}
            className="px-3 py-1.5 rounded-md bg-[var(--brand-input-bg)] text-sm text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)] disabled:opacity-50"
          >
            Next
          </button>
        </div>
      )}

      {historyProfile && <ProfileHistory key={historyProfile.id} profile={historyProfile} canEdit={!!canManageProfiles && (currentUser?.role === 'admin' || historyProfile.created_by === currentUser?.id)} onClose={() => setHistoryProfile(null)} />}

      {tagProfile && <div role="dialog" aria-modal="true" aria-label={`Edit tags for ${tagProfile.name}`} className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4">
        <form onSubmit={e => { e.preventDefault(); tagsMutation.mutate({ id: tagProfile.id, tags }) }} className="bg-[var(--brand-card-bg)] rounded-xl p-6 max-w-md w-full">
          <h3 className="text-lg mb-4">Edit profile tags</h3>
          <label className="block">Tags<input value={tags} onChange={e => setTags(e.target.value)} className="block w-full bg-[var(--brand-input-bg)] border rounded-md p-2 mt-2" /></label>
          <p className="text-xs mt-2">Separate tags with commas. Clear the field to remove all tags.</p>
          <div className="flex justify-end gap-2 mt-4"><button type="button" disabled={tagsMutation.isPending} onClick={() => setTagProfile(null)}>Cancel</button><button type="submit" disabled={tagsMutation.isPending}>{tagsMutation.isPending ? 'Saving...' : 'Save tags'}</button></div>
        </form>
      </div>}

      {/* Apply modal */}
      {applyProfile && (
        <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" onClick={() => setApplyProfile(null)}>
          <div className="bg-[var(--brand-card-bg)] rounded-xl border border-[var(--brand-card-border)] p-6 max-w-md w-full" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-medium mb-4">Apply "{applyProfile.name}"</h3>
            <p className="text-sm text-[var(--brand-muted)] mb-4">Select a Klipper printer. This sends GCode immediately.</p>
            <select
              aria-label="Apply to printer"
              value={applyPrinterId}
              onChange={e => setApplyPrinterId(e.target.value)}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm mb-4"
            >
              <option value="">Select printer...</option>
              {klipperPrinters.map(p => (
                <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
              ))}
            </select>
            <div className="flex justify-end gap-2">
              <button onClick={() => setApplyProfile(null)} className="px-4 py-2 text-sm text-[var(--brand-muted)] hover:text-white">Cancel</button>
              <button
                onClick={() => applyMutation.mutate({ profileId: applyProfile.id, printerId: parseInt(applyPrinterId) })}
                disabled={!applyPrinterId || applyMutation.isPending}
                className="px-4 py-2 bg-green-600 hover:bg-green-500 rounded-md text-sm font-medium disabled:opacity-50"
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
