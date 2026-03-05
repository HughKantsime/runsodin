import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Beaker, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { canDo } from '../../permissions'
import { useOrg } from '../../contexts/OrgContext'
import { bulkOps, spools as spoolsApi, filaments as filamentApi, printers as printersApi } from '../../api'
import { PageHeader, StatCard, SearchInput, TabBar, Button, EmptyState } from '../../components/ui'
import ConfirmModal from '../../components/shared/ConfirmModal'
import FilamentLibraryView from '../../components/inventory/FilamentLibraryView'
import SpoolGrid from '../../components/inventory/SpoolGrid'
import { CreateSpoolModal, LoadSpoolModal, UseSpoolModal, DryingModal } from '../../components/inventory/SpoolModals'
import { EditSpoolModal } from '../../components/inventory/SpoolEditModals'

export default function Spools() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [view, setView] = useState('spools') // 'spools' | 'library'
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [loadingSpool, setLoadingSpool] = useState(null)
  const [usingSpool, setUsingSpool] = useState(null)
  const [editingSpool, setEditingSpool] = useState(null)
  const [dryingSpool, setDryingSpool] = useState(null)
  const [filter, _setFilter] = useState(() => searchParams.get('status') || 'active')
  const [sortBy, _setSortBy] = useState(() => searchParams.get('sort') || 'printer')
  const [groupByPrinter, setGroupByPrinter] = useState(true)
  const [searchQuery, _setSearchQuery] = useState(() => searchParams.get('q') || '')

  const updateSearchParams = useCallback((updates) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      Object.entries(updates).forEach(([key, value]) => {
        if (value && value !== '' && !(key === 'status' && value === 'active') && !(key === 'sort' && value === 'printer')) {
          next.set(key, value)
        } else {
          next.delete(key)
        }
      })
      return next
    }, { replace: true })
  }, [setSearchParams])

  const setFilter = useCallback((value) => {
    _setFilter(value)
    updateSearchParams({ status: value })
  }, [updateSearchParams])

  const setSortBy = useCallback((value) => {
    _setSortBy(value)
    updateSearchParams({ sort: value })
  }, [updateSearchParams])

  const setSearchQuery = useCallback((value) => {
    _setSearchQuery(value)
    updateSearchParams({ q: value })
  }, [updateSearchParams])

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
      <PageHeader icon={Package} title="Spools" subtitle="Track your filament inventory">
        <TabBar
          variant="segment"
          tabs={[
            { value: 'spools', label: 'Spools' },
            { value: 'library', label: 'Filament Library', icon: Beaker },
          ]}
          active={view}
          onChange={setView}
        />
        {view === 'spools' && canDo('spools.edit') && (
          <Button icon={Plus} onClick={() => setShowCreateModal(true)}>
            Add Spool
          </Button>
        )}
      </PageHeader>

      {/* Filament Library View */}
      {view === 'library' && <FilamentLibraryView />}

      {/* Spools View */}
      {view === 'spools' && (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 md:mb-6">
            <StatCard label="Active Spools" value={activeSpools.length} icon={Package} />
            <StatCard label="Loaded" value={loadedSpools.length} color="blue" />
            <StatCard label="Low (<20%)" value={lowSpools.length} color="amber" />
            <StatCard
              label="Total Filament"
              value={`${activeSpools.reduce((sum, s) => sum + (s.remaining_weight_g || 0), 0).toFixed(0)}g`}
            />
          </div>

          {/* Low warning */}
          {lowSpools.length > 0 && (
            <button
              onClick={() => { setFilter('active'); setSortBy('remaining') }}
              className="mb-4 md:mb-6 p-3 md:p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-md flex items-center gap-3 w-full text-left hover:bg-yellow-500/20 transition-colors"
            >
              <AlertTriangle className="text-yellow-500 flex-shrink-0" size={20} />
              <span className="text-yellow-200 text-sm md:text-base">
                {lowSpools.length} spool{lowSpools.length > 1 ? 's' : ''} running low on filament
              </span>
            </button>
          )}

          {/* Search */}
          <div className="mb-4 md:mb-6">
            <SearchInput
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search by brand, name, or material..."
            />
          </div>

          {/* Filter tabs + Sort controls */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 mb-4 md:mb-6">
            <TabBar
              tabs={[
                { value: 'active', label: 'Active' },
                { value: 'empty', label: 'Empty' },
                { value: 'archived', label: 'Archived' },
                { value: 'all', label: 'All' },
              ]}
              active={filter}
              onChange={setFilter}
            />

            <div className="flex gap-3 items-center sm:ml-auto">
              <span className="text-xs md:text-sm text-[var(--brand-text-muted)]">Sort:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 md:px-3 py-1 md:py-1.5 text-xs md:text-sm text-[var(--brand-text-secondary)]"
              >
                <option value="printer">Printer/Slot</option>
                <option value="name">Name</option>
                <option value="remaining">Remaining %</option>
                <option value="material">Material</option>
              </select>
              <label className="flex items-center gap-1.5 md:gap-2 text-xs md:text-sm text-[var(--brand-text-muted)]">
                <input
                  type="checkbox"
                  checked={groupByPrinter}
                  onChange={(e) => setGroupByPrinter(e.target.checked)}
                  className="rounded-md bg-[var(--brand-input-bg)] border-[var(--brand-card-border)]"
                />
                <span className="hidden sm:inline">Group by printer</span>
                <span className="sm:hidden">Group</span>
              </label>
            </div>
          </div>

          {selectedSpools.size > 0 && canDo('spools.edit') && (
            <div className="flex items-center gap-3 mb-4 p-3 bg-[var(--brand-primary)]/10 border border-[var(--brand-primary)]/30 rounded-md">
              <span className="text-sm text-[var(--brand-text-secondary)]">{selectedSpools.size} selected</span>
              <Button variant="warning" size="sm" onClick={() => bulkSpoolAction.mutate({ action: 'archive' })}>Archive</Button>
              <Button variant="success" size="sm" onClick={() => bulkSpoolAction.mutate({ action: 'activate' })}>Activate</Button>
              <Button variant="danger" size="sm" onClick={() => setConfirmAction({ title: 'Delete Spools', message: `Delete ${selectedSpools.size} selected spool(s)? This cannot be undone.`, onConfirm: () => { bulkSpoolAction.mutate({ action: 'delete' }); setConfirmAction(null) } })}>Delete</Button>
              <Button variant="secondary" size="sm" onClick={() => setSelectedSpools(new Set())}>Clear</Button>
            </div>
          )}
          {canDo('spools.edit') && spools?.length > 0 && (
            <div className="flex items-center gap-2 mb-3">
              <label className="flex items-center gap-1.5 text-xs text-[var(--brand-text-muted)] cursor-pointer">
                <input type="checkbox" checked={selectedSpools.size === spools.length && spools.length > 0} onChange={() => toggleSelectAllSpools(spools.map(s => s.id))} className="rounded border-[var(--brand-card-border)]" />
                Select all
              </label>
            </div>
          )}

          {isLoading && <div className="text-center text-[var(--brand-text-muted)] py-12">Loading spools...</div>}
          {!isLoading && spools?.length === 0 && (
            <EmptyState
              icon={Package}
              title="No spools found"
              description="Add your first spool to get started!"
            >
              {canDo('spools.edit') && (
                <Button icon={Plus} onClick={() => setShowCreateModal(true)}>Add Spool</Button>
              )}
            </EmptyState>
          )}
          {!isLoading && spools?.length > 0 && (
            <SpoolGrid
              spools={spools}
              printers={printers}
              sortBy={sortBy}
              groupByPrinter={groupByPrinter}
              searchQuery={searchQuery}
              selectedSpools={selectedSpools}
              onToggleSelect={toggleSpoolSelect}
              onLoad={setLoadingSpool}
              onUnload={handleUnload}
              onUse={setUsingSpool}
              onArchive={handleArchive}
              onEdit={setEditingSpool}
              onDry={setDryingSpool}
            />
          )}
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
