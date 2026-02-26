import QRScannerModal from '../../components/inventory/QRScannerModal';
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, ArrowUpDown, Filter, Printer as PrinterIcon } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import { getPlugState, plugPowerOn, plugPowerOff } from '../../api'
import { printers, filaments, bulkOps, spools as spoolsApi } from '../../api'
import { canDo } from '../../permissions'
import { useLicense } from '../../LicenseContext'
import UpgradeModal from '../../components/shared/UpgradeModal'
import { useOrg } from '../../contexts/OrgContext'
import CameraModal from '../../components/printers/CameraModal'
import PrinterCard from '../../components/printers/PrinterCard'
import PrinterModal from '../../components/printers/PrinterModal'

export default function Printers() {
  const [deleteConfirmId, setDeleteConfirmId] = useState(null)
  const [cameraTarget, setCameraTarget] = useState(null)
  const { data: activeCameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: () => printers.getCameras().catch(() => [])
  })
  const cameraIds = new Set((activeCameras || []).map(c => c.id))
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [showUpgradeModal, setShowUpgradeModal] = useState(false)
  const [plugStates, setPlugStates] = useState({})

  const handlePlugToggle = async (printerId) => {
    const isOn = plugStates[printerId]
    try {
      if (isOn) {
        await plugPowerOff(printerId)
      } else {
        await plugPowerOn(printerId)
      }
      setPlugStates(prev => ({ ...prev, [printerId]: !isOn }))
    } catch (e) {
      console.error('Plug toggle failed:', e)
    }
  }
  const [editingPrinter, setEditingPrinter] = useState(null)
  const [cardSize, setCardSize] = useState(() => localStorage.getItem('printerCardSize') || 'M')
  const [orderedPrinters, setOrderedPrinters] = useState([])
  const [draggedId, setDraggedId] = useState(null)
  const [showScanner, setShowScanner] = useState(false)
  const [scannerPrinterId, setScannerPrinterId] = useState(null)
  const [searchTerm, setSearchTerm] = useState(() => sessionStorage.getItem('printers_search') || '')
  const [statusFilter, setStatusFilter] = useState(() => sessionStorage.getItem('printers_status') || 'all')
  const [typeFilter, setTypeFilter] = useState(() => sessionStorage.getItem('printers_type') || 'all')
  const [sortBy, setSortBy] = useState(() => sessionStorage.getItem('printers_sort') || 'manual')
  const [tagFilter, setTagFilter] = useState('')

  const org = useOrg()
  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers', org.orgId], queryFn: () => printers.list(false, '', org.orgId) })
  const lic = useLicense()
  const atLimit = !lic.isPro && (printersData?.length || 0) >= 5
  const { data: filamentsData } = useQuery({ queryKey: ['filaments-combined'], queryFn: () => filaments.combined() })
  const { data: spoolsData } = useQuery({ queryKey: ['spools'], queryFn: () => spoolsApi.list({ status: 'active' }) })

  const createPrinter = useMutation({ mutationFn: printers.create, onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setShowModal(false) } })
  const updatePrinter = useMutation({ mutationFn: ({ id, data }) => printers.update(id, data), onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setShowModal(false); setEditingPrinter(null) } })
  const deletePrinter = useMutation({ mutationFn: printers.delete, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })
  const updateSlot = useMutation({ mutationFn: ({ printerId, slotNumber, data }) => printers.updateSlot(printerId, slotNumber, data), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })
  const reorderPrinters = useMutation({ mutationFn: printers.reorder, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })

  // Bulk selection
  const [selectedPrinters, setSelectedPrinters] = useState(new Set())
  const togglePrinterSelect = (id) => setSelectedPrinters(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const toggleSelectAllPrinters = (ids) => {
    setSelectedPrinters(prev => prev.size === ids.length ? new Set() : new Set(ids))
  }
  const bulkPrinterAction = useMutation({
    mutationFn: ({ action, extra }) => bulkOps.printers([...selectedPrinters], action, extra),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setSelectedPrinters(new Set()) },
  })

  useEffect(() => {
    if (printersData) setOrderedPrinters(printersData)
  }, [printersData])
  useEffect(() => {
    if (printersData) {
      printersData.filter(p => p.plug_type).forEach(async (p) => {
        try {
          const state = await getPlugState(p.id)
          setPlugStates(prev => ({ ...prev, [p.id]: state?.is_on || false }))
        } catch (e) {}
      })
    }
  }, [printersData])

  // Persist filter state to sessionStorage
  useEffect(() => { sessionStorage.setItem('printers_search', searchTerm) }, [searchTerm])
  useEffect(() => { sessionStorage.setItem('printers_status', statusFilter) }, [statusFilter])
  useEffect(() => { sessionStorage.setItem('printers_type', typeFilter) }, [typeFilter])
  useEffect(() => { sessionStorage.setItem('printers_sort', sortBy) }, [sortBy])

  // Derive unique api_types from printers
  const apiTypes = [...new Set((printersData || []).map(p => p.api_type).filter(Boolean))]

  const allTags = [...new Set((printersData || []).flatMap(p => p.tags || []))].sort()
  const isFiltered = searchTerm || statusFilter !== 'all' || typeFilter !== 'all' || sortBy !== 'manual' || tagFilter

  const filteredPrinters = (() => {
    let list = [...(orderedPrinters || [])]
    if (searchTerm) {
      const q = searchTerm.toLowerCase()
      list = list.filter(p =>
        (p.name || '').toLowerCase().includes(q) ||
        (p.nickname || '').toLowerCase().includes(q) ||
        (p.model || '').toLowerCase().includes(q)
      )
    }
    if (statusFilter !== 'all') {
      list = list.filter(p => {
        const online = p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000
        const stage = p.print_stage && p.print_stage !== 'Idle' ? p.print_stage : null
        switch (statusFilter) {
          case 'online': return online
          case 'offline': return !online
          case 'printing': return stage === 'Running' || stage === 'Printing'
          case 'idle': return online && (!stage || stage === 'Idle')
          default: return true
        }
      })
    }
    if (typeFilter !== 'all') {
      list = list.filter(p => p.api_type === typeFilter)
    }
    if (tagFilter) {
      list = list.filter(p => p.tags?.includes(tagFilter))
    }
    if (sortBy !== 'manual') {
      list.sort((a, b) => {
        switch (sortBy) {
          case 'name_asc': return (a.nickname || a.name || '').localeCompare(b.nickname || b.name || '')
          case 'name_desc': return (b.nickname || b.name || '').localeCompare(a.nickname || a.name || '')
          case 'status': {
            const aOn = a.last_seen && (Date.now() - new Date(a.last_seen + 'Z').getTime()) < 90000 ? 0 : 1
            const bOn = b.last_seen && (Date.now() - new Date(b.last_seen + 'Z').getTime()) < 90000 ? 0 : 1
            return aOn - bOn
          }
          case 'model': return (a.model || '').localeCompare(b.model || '')
          default: return 0
        }
      })
    }
    return list
  })()

  const handleDragStart = (e, printerId) => {
    setDraggedId(printerId)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragOver = (e, targetId) => {
    e.preventDefault()
    if (draggedId === null || draggedId === targetId) return
    const draggedIndex = orderedPrinters.findIndex(p => p.id === draggedId)
    const targetIndex = orderedPrinters.findIndex(p => p.id === targetId)
    if (draggedIndex === targetIndex) return
    const newOrder = [...orderedPrinters]
    const [dragged] = newOrder.splice(draggedIndex, 1)
    newOrder.splice(targetIndex, 0, dragged)
    setOrderedPrinters(newOrder)
  }

  const handleDragEnd = () => {
    if (draggedId !== null) reorderPrinters.mutate(orderedPrinters.map(p => p.id))
    setDraggedId(null)
  }

  const handleSubmit = (data, printerId) => {
    if (printerId) {
      updatePrinter.mutate({ id: printerId, data })
    } else {
      createPrinter.mutate(data)
    }
  }

  const handleEdit = (printer) => {
    setEditingPrinter(printer)
    setShowModal(true)
  }

  const handleCloseModal = () => {
    setShowModal(false)
    setEditingPrinter(null)
  }

  const handleSyncAms = async (printerId) => {
    try {
      await printers.syncAms(printerId)
      queryClient.invalidateQueries({ queryKey: ['printers'] })
    } catch (err) {
      toast.error('Failed to sync AMS')
    }
  }

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div className="flex items-center gap-3">
          <PrinterIcon className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Printers</h1>
            <p className="text-farm-500 text-sm mt-1">Manage your print farm</p>
          </div>
        </div>
        {canDo('printers.add') && (atLimit
          ? <button onClick={() => setShowUpgradeModal(true)} className="flex items-center gap-2 px-4 py-2 bg-farm-700 text-farm-400 hover:text-farm-300 rounded-lg text-sm self-start transition-colors" title={`Printer limit reached (${lic.max_printers || 5}). Upgrade to Pro for unlimited.`}>
              <Plus size={16} /> Add Printer (limit: {lic.max_printers || 5})
            </button>
          : <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm self-start">
              <Plus size={16} /> Add Printer
            </button>
        )}
      </div>
      {/* Filter Toolbar */}
      {printersData?.length > 0 && (
        <div className="bg-farm-900 border border-farm-800 rounded-lg p-3 mb-4 md:mb-6 flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-farm-500" />
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search printers..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg pl-8 pr-3 py-1.5 text-sm"
            />
          </div>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
            <option value="all">All Status</option>
            <option value="online">Online</option>
            <option value="offline">Offline</option>
            <option value="printing">Printing</option>
            <option value="idle">Idle</option>
          </select>
          {apiTypes.length > 1 && (
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
              <option value="all">All Types</option>
              {apiTypes.map(t => (
                <option key={t} value={t}>{t === 'bambu' ? 'Bambu' : t === 'moonraker' ? 'Moonraker' : t === 'prusalink' ? 'PrusaLink' : t === 'elegoo' ? 'Elegoo' : t}</option>
              ))}
            </select>
          )}
          {allTags.length > 0 && (
            <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
              <option value="">All Tags</option>
              {allTags.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          )}
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
            <option value="manual">Manual Order</option>
            <option value="name_asc">Name A-Z</option>
            <option value="name_desc">Name Z-A</option>
            <option value="status">Status (online first)</option>
            <option value="model">Model</option>
          </select>
          {isFiltered && (
            <span className="text-xs text-farm-400">
              Showing {filteredPrinters.length} of {orderedPrinters.length} printers
            </span>
          )}
          <div className="flex items-center gap-0.5 ml-auto border border-farm-700 rounded-lg overflow-hidden">
            {['S', 'M', 'L', 'XL'].map(size => (
              <button
                key={size}
                onClick={() => { setCardSize(size); localStorage.setItem('printerCardSize', size) }}
                className={`px-2 py-1.5 text-xs font-medium transition-colors ${cardSize === size ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'}`}
              >
                {size}
              </button>
            ))}
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12 text-farm-500 text-sm">Loading printers...</div>
      ) : printersData?.length === 0 ? (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-8 md:p-12 text-center">
          <p className="text-farm-500 mb-4">No printers configured yet.</p>
          {canDo('printers.add') && !atLimit && <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Add Your First Printer</button>}
        </div>
      ) : (
        <>
        {selectedPrinters.size > 0 && canDo('printers.edit') && (
          <div className="flex items-center gap-3 mb-4 p-3 bg-print-900/50 border border-print-700 rounded-lg">
            <span className="text-sm text-farm-300">{selectedPrinters.size} selected</span>
            <button onClick={() => bulkPrinterAction.mutate({ action: 'enable' })} className="px-3 py-1 bg-green-600 hover:bg-green-500 rounded text-xs">Enable</button>
            <button onClick={() => bulkPrinterAction.mutate({ action: 'disable' })} className="px-3 py-1 bg-amber-600 hover:bg-amber-500 rounded text-xs">Disable</button>
            <button onClick={() => setSelectedPrinters(new Set())} className="px-3 py-1 bg-farm-700 hover:bg-farm-600 rounded text-xs">Clear</button>
          </div>
        )}
        {canDo('printers.edit') && filteredPrinters.length > 0 && (
          <div className="flex items-center gap-2 mb-3">
            <label className="flex items-center gap-1.5 text-xs text-farm-400 cursor-pointer">
              <input type="checkbox" checked={selectedPrinters.size === filteredPrinters.length && filteredPrinters.length > 0} onChange={() => toggleSelectAllPrinters(filteredPrinters.map(p => p.id))} className="rounded border-farm-600" />
              Select all
            </label>
          </div>
        )}
        <div className={clsx('grid gap-4 md:gap-6 items-start', {
          'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4': cardSize === 'S',
          'grid-cols-1 lg:grid-cols-2 xl:grid-cols-3': cardSize === 'M',
          'grid-cols-1 lg:grid-cols-2': cardSize === 'L',
          'grid-cols-1': cardSize === 'XL',
        })}>
          {filteredPrinters.map((printer) => (
            <div key={printer.id} className="relative">
              {canDo('printers.edit') && (
                <input
                  type="checkbox"
                  checked={selectedPrinters.has(printer.id)}
                  onChange={() => togglePrinterSelect(printer.id)}
                  className="absolute top-3 left-3 z-10 rounded border-farm-600"
                />
              )}
              <PrinterCard
                printer={printer}
                allFilaments={filamentsData}
                spools={spoolsData}
                onDelete={(id) => setDeleteConfirmId(id)}
                onToggleActive={(id, active) => updatePrinter.mutate({ id, data: { is_active: active } })}
                onUpdateSlot={(pid, slot, data) => updateSlot.mutate({ printerId: pid, slotNumber: slot, data })}
                onEdit={handleEdit}
                onSyncAms={handleSyncAms}
                hasCamera={cameraIds.has(printer.id)}
                onCameraClick={setCameraTarget}
                isDragging={draggedId === printer.id}
                onDragStart={isFiltered ? undefined : (e) => handleDragStart(e, printer.id)}
                onDragOver={isFiltered ? undefined : (e) => handleDragOver(e, printer.id)}
                onDragEnd={isFiltered ? undefined : handleDragEnd}
                onScanSpool={() => { setScannerPrinterId(printer.id); setShowScanner(true); }}
                onPlugToggle={handlePlugToggle}
                plugStates={plugStates}
              />
            </div>
          ))}
          {filteredPrinters.length === 0 && orderedPrinters.length > 0 && (
            <div className="col-span-full bg-farm-900 rounded-lg border border-farm-800 p-8 text-center">
              <p className="text-farm-500 text-sm">No printers match your filters.</p>
            </div>
          )}
        </div>
        </>
      )}
      <PrinterModal isOpen={showModal} onClose={handleCloseModal} onSubmit={handleSubmit} printer={editingPrinter} />
      {cameraTarget && <CameraModal printer={cameraTarget} onClose={() => setCameraTarget(null)} />}
      {showScanner && (
        <QRScannerModal
          isOpen={showScanner}
          onClose={() => setShowScanner(false)}
          preselectedPrinter={scannerPrinterId}
          onAssigned={() => {
            setShowScanner(false);
            queryClient.invalidateQueries({ queryKey: ['printers'] });
          }}
        />
      )}
      <UpgradeModal isOpen={showUpgradeModal} onClose={() => setShowUpgradeModal(false)} resource="printers" />
      <ConfirmModal
        open={!!deleteConfirmId}
        onConfirm={() => { deletePrinter.mutate(deleteConfirmId); setDeleteConfirmId(null) }}
        onCancel={() => setDeleteConfirmId(null)}
        title="Delete Printer"
        message="Permanently delete this printer? This cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
      />
    </div>
  )
}
