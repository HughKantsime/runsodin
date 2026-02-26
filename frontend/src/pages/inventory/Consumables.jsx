import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Trash2, Pencil, X, AlertTriangle, ArrowUpDown } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import { consumables } from '../../api'
import { canDo } from '../../permissions'
import ConfirmModal from '../../components/ConfirmModal'

const UNIT_OPTIONS = ['piece', 'gram', 'ml', 'meter', 'pack', 'box', 'sheet']

export default function Consumables() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [showAdjustModal, setShowAdjustModal] = useState(null)
  const [editingItem, setEditingItem] = useState(null)
  const [searchText, setSearchText] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [formData, setFormData] = useState({
    name: '', sku: '', unit: 'piece', cost_per_unit: '', current_stock: '',
    min_stock: '', vendor: '', notes: '', status: 'active'
  })
  const [adjustData, setAdjustData] = useState({ quantity: '', type: 'restock', notes: '' })
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [confirmLargeAdjust, setConfirmLargeAdjust] = useState(false)

  const { data: items = [], isLoading } = useQuery({
    queryKey: ['consumables', statusFilter],
    queryFn: () => consumables.list(statusFilter || undefined),
  })

  const createMutation = useMutation({
    mutationFn: (data) => consumables.create(data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['consumables'] }); handleCloseModal() },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => consumables.update(id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['consumables'] }); handleCloseModal() },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => consumables.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['consumables'] }),
  })

  const adjustMutation = useMutation({
    mutationFn: ({ id, data }) => consumables.adjust(id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['consumables'] }); setShowAdjustModal(null) },
  })

  const handleCloseModal = () => {
    setShowModal(false)
    setEditingItem(null)
    setFormData({ name: '', sku: '', unit: 'piece', cost_per_unit: '', current_stock: '', min_stock: '', vendor: '', notes: '', status: 'active' })
  }

  const handleEdit = (item) => {
    setEditingItem(item)
    setFormData({
      name: item.name, sku: item.sku || '', unit: item.unit || 'piece',
      cost_per_unit: item.cost_per_unit || '', current_stock: item.current_stock || '',
      min_stock: item.min_stock || '', vendor: item.vendor || '', notes: item.notes || '',
      status: item.status || 'active'
    })
    setShowModal(true)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    const payload = {
      ...formData,
      cost_per_unit: parseFloat(formData.cost_per_unit) || 0,
      current_stock: parseFloat(formData.current_stock) || 0,
      min_stock: parseFloat(formData.min_stock) || 0,
    }
    if (editingItem) {
      updateMutation.mutate({ id: editingItem.id, data: payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  const LARGE_ADJUST_THRESHOLD = 100

  const handleAdjust = (e) => {
    e.preventDefault()
    const qty = parseFloat(adjustData.quantity)
    if (qty >= LARGE_ADJUST_THRESHOLD && !confirmLargeAdjust) {
      setConfirmLargeAdjust(true)
      return
    }
    setConfirmLargeAdjust(false)
    adjustMutation.mutate({
      id: showAdjustModal.id,
      data: { quantity: qty, type: adjustData.type, notes: adjustData.notes }
    })
  }

  const filtered = items.filter(i =>
    (!searchText || i.name.toLowerCase().includes(searchText.toLowerCase()) || (i.sku && i.sku.toLowerCase().includes(searchText.toLowerCase())))
  )

  const lowStockCount = items.filter(i => i.is_low_stock).length

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
        <div>
          <div className="flex items-center gap-3">
            <Package className="text-print-400" size={24} />
            <div>
              <h1 className="text-xl md:text-2xl font-display font-bold">Consumables</h1>
              <p className="text-farm-500 text-sm mt-1">Non-printed inventory items for product assembly</p>
            </div>
          </div>
        </div>
        {/* Consumables share models.* permissions — no separate consumables RBAC tier */}
        {canDo('models.create') && (
          <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 text-white rounded-lg hover:bg-print-500 transition-colors">
            <Plus size={18} /> Add Consumable
          </button>
        )}
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="rounded-lg p-4 bg-farm-900 border border-farm-800">
          <div className="text-2xl font-bold text-farm-100">{items.length}</div>
          <div className="text-xs text-farm-400">Total Items</div>
        </div>
        <div className="rounded-lg p-4 bg-farm-900 border border-farm-800">
          <div className="text-2xl font-bold text-yellow-400">{lowStockCount}</div>
          <div className="text-xs text-farm-400">Low Stock</div>
        </div>
        <div className="rounded-lg p-4 bg-farm-900 border border-farm-800">
          <div className="text-2xl font-bold text-farm-100">
            {items.filter(i => i.status === 'active').length}
          </div>
          <div className="text-xs text-farm-400">Active</div>
        </div>
        <div className="rounded-lg p-4 bg-farm-900 border border-farm-800">
          <div className="text-2xl font-bold text-farm-100">
            ${items.reduce((sum, i) => sum + (i.cost_per_unit || 0) * (i.current_stock || 0), 0).toFixed(2)}
          </div>
          <div className="text-xs text-farm-400">Inventory Value</div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <input
          type="text" placeholder="Search name or SKU..." value={searchText}
          onChange={e => setSearchText(e.target.value)}
          className="px-3 py-2 bg-farm-800 border border-farm-600 rounded-lg text-sm w-64 text-farm-100"
        />
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
          className="px-3 py-2 bg-farm-800 border border-farm-600 rounded-lg text-sm text-farm-100">
          <option value="">All Status</option>
          <option value="active">Active</option>
          <option value="depleted">Depleted</option>
          <option value="discontinued">Discontinued</option>
        </select>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading...</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          {items.length === 0 ? 'No consumables yet. Add your first item.' : 'No results match your search.'}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-farm-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-farm-900">
                <th className="text-left p-3 text-farm-400 font-medium">Name</th>
                <th className="text-left p-3 text-farm-400 font-medium hidden md:table-cell">SKU</th>
                <th className="text-left p-3 text-farm-400 font-medium">Stock</th>
                <th className="text-left p-3 text-farm-400 font-medium hidden md:table-cell">Unit</th>
                <th className="text-left p-3 text-farm-400 font-medium hidden md:table-cell">Cost/Unit</th>
                <th className="text-left p-3 text-farm-400 font-medium hidden lg:table-cell">Vendor</th>
                <th className="text-right p-3 text-farm-400 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(item => (
                <tr key={item.id} className="border-t border-farm-800 hover:bg-farm-800/30 transition-colors">
                  <td className="p-3">
                    <div className="flex items-center gap-2">
                      {item.is_low_stock && <AlertTriangle size={14} className="text-yellow-400 shrink-0" />}
                      <span>{item.name}</span>
                    </div>
                  </td>
                  <td className="p-3 text-farm-400 hidden md:table-cell">{item.sku || '—'}</td>
                  <td className="p-3">
                    <span className={clsx('font-medium', item.is_low_stock ? 'text-yellow-400' : 'text-farm-100')}>
                      {item.current_stock}
                    </span>
                    {item.min_stock > 0 && <span className="text-farm-500 text-xs ml-1">/ min {item.min_stock}</span>}
                  </td>
                  <td className="p-3 text-farm-400 hidden md:table-cell">{item.unit}</td>
                  <td className="p-3 text-farm-400 hidden md:table-cell">{item.cost_per_unit ? `$${item.cost_per_unit.toFixed(2)}` : '—'}</td>
                  <td className="p-3 text-farm-400 hidden lg:table-cell">{item.vendor || '—'}</td>
                  <td className="p-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => { setShowAdjustModal(item); setAdjustData({ quantity: '', type: 'restock', notes: '' }) }}
                        className="p-1.5 text-farm-400 hover:bg-farm-700 rounded-lg" title="Adjust Stock">
                        <ArrowUpDown size={14} />
                      </button>
                      {canDo('models.update') && (
                        <button onClick={() => handleEdit(item)} className="p-1.5 text-farm-400 hover:bg-farm-700 rounded-lg" title="Edit">
                          <Pencil size={14} />
                        </button>
                      )}
                      {canDo('models.delete') && (
                        <button onClick={() => setConfirmDelete(item)}
                          className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg" title="Delete">
                          <Trash2 size={14} />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="rounded-xl p-6 w-full max-w-md bg-farm-950 border border-farm-800">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">
                {editingItem ? 'Edit Consumable' : 'Add Consumable'}
              </h3>
              <button onClick={handleCloseModal} className="text-farm-500 hover:text-farm-300"><X size={20} /></button>
            </div>
            <form onSubmit={handleSubmit} className="space-y-3">
              <div>
                <label className="text-xs text-farm-400">Name *</label>
                <input type="text" required value={formData.name} onChange={e => setFormData(f => ({ ...f, name: e.target.value }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-farm-400">SKU</label>
                  <input type="text" value={formData.sku} onChange={e => setFormData(f => ({ ...f, sku: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
                </div>
                <div>
                  <label className="text-xs text-farm-400">Unit</label>
                  <select value={formData.unit} onChange={e => setFormData(f => ({ ...f, unit: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm">
                    {UNIT_OPTIONS.map(u => <option key={u} value={u}>{u}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="text-xs text-farm-400">Cost/Unit ($)</label>
                  <input type="number" step="0.01" value={formData.cost_per_unit} onChange={e => setFormData(f => ({ ...f, cost_per_unit: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
                </div>
                <div>
                  <label className="text-xs text-farm-400">Current Stock</label>
                  <input type="number" step="0.01" value={formData.current_stock} onChange={e => setFormData(f => ({ ...f, current_stock: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
                </div>
                <div>
                  <label className="text-xs text-farm-400">Min Stock</label>
                  <input type="number" step="0.01" value={formData.min_stock} onChange={e => setFormData(f => ({ ...f, min_stock: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
                </div>
              </div>
              <div>
                <label className="text-xs text-farm-400">Vendor</label>
                <input type="text" value={formData.vendor} onChange={e => setFormData(f => ({ ...f, vendor: e.target.value }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="text-xs text-farm-400">Notes</label>
                <textarea value={formData.notes} onChange={e => setFormData(f => ({ ...f, notes: e.target.value }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm h-16 resize-none" />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={handleCloseModal} className="px-4 py-2 bg-farm-700 text-farm-300 rounded-lg text-sm hover:bg-farm-600">Cancel</button>
                <button type="submit" className="px-4 py-2 bg-print-600 text-white rounded-lg text-sm hover:bg-print-500">
                  {editingItem ? 'Save' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Stock Adjustment Modal */}
      {showAdjustModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="rounded-xl p-6 w-full max-w-sm bg-farm-950 border border-farm-800">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Adjust Stock</h3>
              <button onClick={() => { setShowAdjustModal(null); setConfirmLargeAdjust(false) }} className="text-farm-500 hover:text-farm-300"><X size={20} /></button>
            </div>
            <p className="text-sm text-farm-400 mb-3">{showAdjustModal.name} — current: {showAdjustModal.current_stock} {showAdjustModal.unit}s</p>
            <form onSubmit={handleAdjust} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-farm-400">Type</label>
                  <select value={adjustData.type} onChange={e => setAdjustData(d => ({ ...d, type: e.target.value }))}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm">
                    <option value="restock">Restock</option>
                    <option value="deduct">Deduct</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs text-farm-400">Quantity</label>
                  <input type="number" step="0.01" required value={adjustData.quantity}
                    onChange={e => { setAdjustData(d => ({ ...d, quantity: e.target.value })); setConfirmLargeAdjust(false) }}
                    className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
                </div>
              </div>
              {confirmLargeAdjust && (
                <div className="p-2 bg-amber-900/30 border border-amber-700/50 rounded-lg text-xs text-amber-300 flex items-center gap-2">
                  <AlertTriangle size={14} className="shrink-0" />
                  Large adjustment ({adjustData.quantity} {showAdjustModal.unit}s). Click Adjust again to confirm.
                </div>
              )}
              <div>
                <label className="text-xs text-farm-400">Notes</label>
                <input type="text" value={adjustData.notes} onChange={e => setAdjustData(d => ({ ...d, notes: e.target.value }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => { setShowAdjustModal(null); setConfirmLargeAdjust(false) }} className="px-4 py-2 bg-farm-700 text-farm-300 rounded-lg text-sm">Cancel</button>
                <button type="submit" className="px-4 py-2 bg-print-600 text-white rounded-lg text-sm hover:bg-print-500">Adjust</button>
              </div>
            </form>
          </div>
        </div>
      )}

      <ConfirmModal
        open={!!confirmDelete}
        title="Delete Consumable"
        message={confirmDelete ? `Are you sure you want to delete "${confirmDelete.name}"? This cannot be undone.` : ''}
        confirmText="Delete"
        confirmVariant="danger"
        onConfirm={() => { deleteMutation.mutate(confirmDelete.id); setConfirmDelete(null) }}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  )
}
