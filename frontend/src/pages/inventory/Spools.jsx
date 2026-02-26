import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Beaker, AlertTriangle, Search } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import { canDo } from '../../permissions'
import { useOrg } from '../../contexts/OrgContext'
import { bulkOps, spools as spoolsApi, filaments as filamentApi, printers as printersApi } from '../../api'
import ConfirmModal from '../../components/shared/ConfirmModal'
import SpoolCard from '../../components/inventory/SpoolCard'
import FilamentLibraryView from '../../components/inventory/FilamentLibraryView'
import { CreateSpoolModal, LoadSpoolModal, UseSpoolModal, DryingModal, EditSpoolModal } from '../../components/inventory/SpoolModals'

export default function Spools() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const [view, setView] = useState('spools') // 'spools' | 'library'
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [loadingSpool, setLoadingSpool] = useState(null)
  const [usingSpool, setUsingSpool] = useState(null)
  const [editingSpool, setEditingSpool] = useState(null)
  const [dryingSpool, setDryingSpool] = useState(null)
  const [filter, setFilter] = useState('active')
  const [sortBy, setSortBy] = useState("printer")
  const [groupByPrinter, setGroupByPrinter] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')

  const { data: spools, isLoading } = useQuery({
    queryKey: ['spools', filter, org.orgId],
    queryFn: () => spoolsApi.list({ status: filter === 'all' ? undefined : filter, org_id: org.orgId })
  })

  const { data: filaments } = useQuery({
    queryKey: ['filaments'],
    queryFn: filamentApi.list
  })

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: printersApi.list
  })

  const createMutation = useMutation({
    mutationFn: spoolsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spools'] })
      setShowCreateModal(false)
      toast.success('Spool created')
    },
    onError: (err) => toast.error(err.message || 'Failed to create spool')
  })

  const loadMutation = useMutation({
    mutationFn: spoolsApi.load,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spools'] })
      queryClient.invalidateQueries({ queryKey: ['printers'] })
      setLoadingSpool(null)
      toast.success('Spool loaded')
    },
    onError: (err) => toast.error(err.message || 'Failed to load spool')
  })

  const unloadMutation = useMutation({
    mutationFn: spoolsApi.unload,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spools'] })
      queryClient.invalidateQueries({ queryKey: ['printers'] })
      toast.success('Spool unloaded')
    },
    onError: (err) => toast.error(err.message || 'Failed to unload spool')
  })

  const useMutation2 = useMutation({
    mutationFn: spoolsApi.use,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spools'] })
      setUsingSpool(null)
      toast.success('Usage recorded')
    },
    onError: (err) => toast.error(err.message || 'Failed to record usage')
  })

  const archiveMutation = useMutation({
    mutationFn: spoolsApi.archive,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spools'] })
      toast.success('Spool archived')
    },
    onError: (err) => toast.error(err.message || 'Failed to archive spool')
  })

  // Bulk selection
  const [selectedSpools, setSelectedSpools] = useState(new Set())
  const toggleSpoolSelect = (id) => setSelectedSpools(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const toggleSelectAllSpools = (ids) => {
    setSelectedSpools(prev => prev.size === ids.length ? new Set() : new Set(ids))
  }
  const bulkSpoolAction = useMutation({
    mutationFn: ({ action }) => bulkOps.spools([...selectedSpools], action),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['spools'] }); setSelectedSpools(new Set()) },
  })

  const [confirmAction, setConfirmAction] = useState(null)

  const handleUnload = (spool) => {
    setConfirmAction({
      title: 'Unload Spool',
      message: `Unload ${spool.filament_brand} ${spool.filament_name} from printer?`,
      onConfirm: () => { unloadMutation.mutate({ id: spool.id }); setConfirmAction(null) }
    })
  }

  const handleEditSpool = async (data) => {
    try {
      await spoolsApi.update(data);
      queryClient.invalidateQueries({ queryKey: ["spools"] });
      setEditingSpool(null);
      toast.success('Spool updated')
    } catch (err) {
      toast.error(err.message || 'Failed to update spool')
    }
  };

  const handleArchive = (spool) => {
    setConfirmAction({
      title: 'Archive Spool',
      message: `Archive ${spool.filament_brand} ${spool.filament_name}? This will mark it as no longer in use.`,
      onConfirm: () => { archiveMutation.mutate(spool.id); setConfirmAction(null) }
    })
  }

  // Summary stats
  const activeSpools = spools?.filter(s => s.status === 'active') || []
  const lowSpools = activeSpools.filter(s => s.percent_remaining < 20)
  const loadedSpools = activeSpools.filter(s => s.location_printer_id)

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 md:mb-6 gap-3">
        <div className="flex items-center gap-3">
          <Package className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Spools</h1>
            <p className="text-farm-500 text-sm mt-1">Track your filament inventory</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* View toggle */}
          <div className="flex bg-farm-800 rounded-lg p-0.5">
            <button
              onClick={() => setView('spools')}
              className={clsx(
                "px-3 py-1.5 rounded-lg text-xs md:text-sm font-medium transition-colors",
                view === 'spools' ? "bg-print-600 text-white" : "text-farm-400 hover:text-farm-200"
              )}
            >
              Spools
            </button>
            <button
              onClick={() => setView('library')}
              className={clsx(
                "px-3 py-1.5 rounded-lg text-xs md:text-sm font-medium transition-colors flex items-center gap-1.5",
                view === 'library' ? "bg-print-600 text-white" : "text-farm-400 hover:text-farm-200"
              )}
            >
              <Beaker size={14} />
              Filament Library
            </button>
          </div>
          {view === 'spools' && canDo('spools.edit') && (
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white text-sm"
            >
              <Plus size={18} />
              Add Spool
            </button>
          )}
        </div>
      </div>

      {/* Filament Library View */}
      {view === 'library' && <FilamentLibraryView />}

      {/* Spools View */}
      {view === 'spools' && (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-4 md:mb-6">
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-farm-100">{activeSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Active Spools</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-print-400">{loadedSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Loaded</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-yellow-400">{lowSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Low (&lt;20%)</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-farm-100">
                {activeSpools.reduce((sum, s) => sum + (s.remaining_weight_g || 0), 0).toFixed(0)}g
              </div>
              <div className="text-xs md:text-sm text-farm-400">Total Filament</div>
            </div>
          </div>

          {/* Low warning */}
          {lowSpools.length > 0 && (
            <button
              onClick={() => { setFilter('active'); setSortBy('remaining') }}
              className="mb-4 md:mb-6 p-3 md:p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-lg flex items-center gap-3 w-full text-left hover:bg-yellow-500/20 transition-colors"
            >
              <AlertTriangle className="text-yellow-500 flex-shrink-0" size={20} />
              <span className="text-yellow-200 text-sm md:text-base">
                {lowSpools.length} spool{lowSpools.length > 1 ? 's' : ''} running low on filament
              </span>
            </button>
          )}

          {/* Search */}
          <div className="mb-4 md:mb-6">
            <div className="relative">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search by brand, name, or material..."
                className="w-full bg-farm-800 border border-farm-700 rounded-lg pl-9 pr-3 py-2 text-sm text-farm-100 placeholder-farm-500"
              />
            </div>
          </div>

          {/* Filter tabs + Sort controls */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 mb-4 md:mb-6">
            <div className="flex gap-1.5 md:gap-2 justify-evenly">
              {['active', 'empty', 'archived', 'all'].map(f => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={clsx(
                    "px-3 md:px-4 py-1.5 md:py-2 rounded-lg text-xs md:text-sm font-medium transition-colors",
                    filter === f
                      ? "bg-print-600 text-white"
                      : "bg-farm-800 text-farm-400 hover:bg-farm-700"
                  )}
                >
                  {f.charAt(0).toUpperCase() + f.slice(1)}
                </button>
              ))}
            </div>

            <div className="flex gap-3 items-center sm:ml-auto">
              <span className="text-xs md:text-sm text-farm-400">Sort:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="bg-farm-800 border border-farm-700 rounded-lg px-2 md:px-3 py-1 md:py-1.5 text-xs md:text-sm text-farm-200"
              >
                <option value="printer">Printer/Slot</option>
                <option value="name">Name</option>
                <option value="remaining">Remaining %</option>
                <option value="material">Material</option>
              </select>
              <label className="flex items-center gap-1.5 md:gap-2 text-xs md:text-sm text-farm-400">
                <input
                  type="checkbox"
                  checked={groupByPrinter}
                  onChange={(e) => setGroupByPrinter(e.target.checked)}
                  className="rounded-lg bg-farm-800 border-farm-700"
                />
                <span className="hidden sm:inline">Group by printer</span>
                <span className="sm:hidden">Group</span>
              </label>
            </div>
          </div>

          {selectedSpools.size > 0 && canDo('spools.edit') && (
            <div className="flex items-center gap-3 mb-4 p-3 bg-print-900/50 border border-print-700 rounded-lg">
              <span className="text-sm text-farm-300">{selectedSpools.size} selected</span>
              <button onClick={() => bulkSpoolAction.mutate({ action: 'archive' })} className="px-3 py-1 bg-amber-600 hover:bg-amber-500 rounded text-xs">Archive</button>
              <button onClick={() => bulkSpoolAction.mutate({ action: 'activate' })} className="px-3 py-1 bg-green-600 hover:bg-green-500 rounded text-xs">Activate</button>
              <button onClick={() => setConfirmAction({ title: 'Delete Spools', message: `Delete ${selectedSpools.size} selected spool(s)? This cannot be undone.`, onConfirm: () => { bulkSpoolAction.mutate({ action: 'delete' }); setConfirmAction(null) } })} className="px-3 py-1 bg-red-600 hover:bg-red-500 rounded text-xs">Delete</button>
              <button onClick={() => setSelectedSpools(new Set())} className="px-3 py-1 bg-farm-700 hover:bg-farm-600 rounded text-xs">Clear</button>
            </div>
          )}
          {canDo('spools.edit') && spools?.length > 0 && (
            <div className="flex items-center gap-2 mb-3">
              <label className="flex items-center gap-1.5 text-xs text-farm-400 cursor-pointer">
                <input type="checkbox" checked={selectedSpools.size === spools.length && spools.length > 0} onChange={() => toggleSelectAllSpools(spools.map(s => s.id))} className="rounded border-farm-600" />
                Select all
              </label>
            </div>
          )}

          {isLoading && <div className="text-center text-farm-400 py-12">Loading spools...</div>}
          {!isLoading && spools?.length === 0 && <div className="text-center text-farm-400 py-12 text-sm md:text-base">No spools found. Add your first spool to get started!</div>}
          {!isLoading && spools?.length > 0 && (
          (() => {
            if (!spools) return null;
            let sorted = [...spools];

            // Apply text search
            if (searchQuery.trim()) {
              const q = searchQuery.toLowerCase()
              sorted = sorted.filter(s =>
                (s.filament_brand || '').toLowerCase().includes(q) ||
                (s.filament_name || '').toLowerCase().includes(q) ||
                (s.filament_material || '').toLowerCase().includes(q)
              )
            }

            if (sortBy === "printer") {
              sorted.sort((a, b) => {
                if (a.location_printer_id !== b.location_printer_id) return (a.location_printer_id || 999) - (b.location_printer_id || 999);
                return (a.location_slot || 999) - (b.location_slot || 999);
              });
            } else if (sortBy === "name") {
              sorted.sort((a, b) => `${a.filament_brand} ${a.filament_name}`.localeCompare(`${b.filament_brand} ${b.filament_name}`));
            } else if (sortBy === "remaining") {
              sorted.sort((a, b) => (a.percent_remaining || 0) - (b.percent_remaining || 0));
            } else if (sortBy === "material") {
              sorted.sort((a, b) => (a.filament_material || "").localeCompare(b.filament_material || ""));
            }

            if (groupByPrinter && sortBy === "printer") {
              const groups = {};
              sorted.forEach(s => {
                const key = s.location_printer_id ? (printers?.find(p => p.id === s.location_printer_id)?.nickname || printers?.find(p => p.id === s.location_printer_id)?.name || `Printer ${s.location_printer_id}`) : "Unassigned";
                if (!groups[key]) groups[key] = [];
                groups[key].push(s);
              });

              return Object.entries(groups).map(([group, groupSpools]) => (
                <div key={group} className="mb-4 md:mb-6">
                  <h3 className="text-base md:text-lg font-semibold text-farm-200 mb-3">{group}</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
                    {groupSpools.map(spool => (
                      <div key={spool.id} className="relative">
                        {canDo('spools.edit') && (
                          <input type="checkbox" checked={selectedSpools.has(spool.id)} onChange={() => toggleSpoolSelect(spool.id)} className="absolute top-3 left-3 z-10 rounded border-farm-600" />
                        )}
                        <div className={canDo('spools.edit') ? 'pl-7' : ''}>
                        <SpoolCard spool={spool} onLoad={setLoadingSpool} onUnload={handleUnload} onUse={setUsingSpool} onArchive={handleArchive} onEdit={setEditingSpool} onDry={setDryingSpool} printers={printers} />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ));
            }

            return (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
                {sorted.map(spool => (
                  <div key={spool.id} className="relative">
                    {canDo('spools.edit') && (
                      <input type="checkbox" checked={selectedSpools.has(spool.id)} onChange={() => toggleSpoolSelect(spool.id)} className="absolute top-3 left-3 z-10 rounded border-farm-600" />
                    )}
                    <SpoolCard spool={spool} onLoad={setLoadingSpool} onUnload={handleUnload} onUse={setUsingSpool} onArchive={handleArchive} onEdit={setEditingSpool} onDry={setDryingSpool} printers={printers} />
                  </div>
                ))}
              </div>
            );
          })())}
        </>
      )}

      {/* Spool Modals */}
      {showCreateModal && (
        <CreateSpoolModal
          filaments={filaments}
          onClose={() => setShowCreateModal(false)}
          onCreate={createMutation.mutate}
        />
      )}

      {loadingSpool && (
        <LoadSpoolModal
          spool={loadingSpool}
          printers={printers}
          onClose={() => setLoadingSpool(null)}
          onLoad={loadMutation.mutate}
        />
      )}

      {usingSpool && (
        <UseSpoolModal
          spool={usingSpool}
          onClose={() => setUsingSpool(null)}
          onUse={useMutation2.mutate}
        />
      )}
      {editingSpool && (
        <EditSpoolModal
          spool={editingSpool}
          onClose={() => setEditingSpool(null)}
          onSave={handleEditSpool}
        />
      )}
      {dryingSpool && (
        <DryingModal
          spool={dryingSpool}
          onClose={() => setDryingSpool(null)}
          onSubmit={async (data) => {
            try {
              const { id, ...rest } = data
              await spoolsApi.logDrying(id, rest)
              toast.success('Drying session logged')
              setDryingSpool(null)
            } catch (err) {
              toast.error(err.message || 'Failed to log drying session')
            }
          }}
        />
      )}
      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmText={confirmAction?.title?.includes('Delete') ? 'Delete' : 'Confirm'}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  )
}
