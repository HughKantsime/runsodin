import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Palette } from 'lucide-react'
import clsx from 'clsx'
import { filaments as filamentApi } from '../../api'
import { canDo } from '../../permissions'
import { EditFilamentModal } from './SpoolModals'

export default function FilamentLibraryView() {
  const queryClient = useQueryClient()
  const [editingFilament, setEditingFilament] = useState(null)
  const [showAddModal, setShowAddModal] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(null)
  const [materialFilter, setMaterialFilter] = useState('all')

  const { data: filaments, isLoading } = useQuery({
    queryKey: ['filaments'],
    queryFn: filamentApi.list
  })

  const createMutation = useMutation({
    mutationFn: filamentApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setShowAddModal(false)
    }
  })

  const updateMutation = useMutation({
    mutationFn: filamentApi.update,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setEditingFilament(null)
    }
  })

  const deleteMutation = useMutation({
    mutationFn: filamentApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setDeleteConfirm(null)
    }
  })

  // Get unique materials for filter
  const materials = [...new Set(filaments?.map(f => f.material) || [])]

  const filtered = filaments?.filter(f =>
    materialFilter === 'all' || f.material === materialFilter
  ) || []

  // Group by brand
  const grouped = {}
  filtered.forEach(f => {
    if (!grouped[f.brand]) grouped[f.brand] = []
    grouped[f.brand].push(f)
  })

  return (
    <>
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 md:mb-6 gap-3">
        <div>
          <p className="text-farm-400 text-sm">{filaments?.length || 0} filament types in library</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={materialFilter}
            onChange={(e) => setMaterialFilter(e.target.value)}
            className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-1.5 text-sm text-farm-200"
          >
            <option value="all">All Materials</option>
            {materials.map(m => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {canDo('spools.edit') && (
            <button
              onClick={() => setShowAddModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white text-sm"
            >
              <Plus size={16} />
              Add Filament
            </button>
          )}
        </div>
      </div>

      {isLoading && <div className="text-center text-farm-400 py-12">Loading filaments...</div>}

      {!isLoading && filtered.length === 0 && (
        <div className="text-center text-farm-500 py-12 text-sm">
          No filaments found. Add filament types to build your library.
        </div>
      )}

      {/* Filament table grouped by brand */}
      {Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([brand, brandFilaments]) => (
        <div key={brand} className="mb-6">
          <h3 className="text-sm font-semibold text-farm-300 mb-2 uppercase tracking-wide">{brand}</h3>
          <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
            {brandFilaments.map((f, idx) => (
              <div
                key={f.id}
                className={clsx(
                  "flex items-center justify-between px-3 md:px-4 py-2.5 md:py-3 gap-3",
                  idx > 0 && "border-t border-farm-800"
                )}
              >
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  {f.color_hex ? (
                    <div
                      className="w-6 h-6 rounded-full border border-farm-600 flex-shrink-0"
                      style={{ backgroundColor: `#${f.color_hex}` }}
                    />
                  ) : (
                    <div className="w-6 h-6 rounded-full border border-farm-700 bg-farm-800 flex-shrink-0 flex items-center justify-center">
                      <Palette size={12} className="text-farm-500" />
                    </div>
                  )}
                  <div className="min-w-0">
                    <span className="text-sm text-farm-100 truncate block">{f.name}</span>
                  </div>
                  <span className="px-2 py-0.5 bg-farm-800 rounded-lg text-xs text-farm-400 flex-shrink-0">
                    {f.material}
                  </span>
                  {f.cost_per_gram && (
                    <span className="px-2 py-0.5 bg-green-900/50 rounded-lg text-xs text-green-400 flex-shrink-0">
                      ${f.cost_per_gram}/g
                    </span>
                  )}
                </div>

                {canDo('spools.edit') && (
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <button
                      onClick={() => setEditingFilament(f)}
                      className="p-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-300 hover:text-farm-100 transition-colors"
                      title="Edit"
                    >
                      <Pencil size={14} />
                    </button>
                    {deleteConfirm === f.id ? (
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => deleteMutation.mutate(f.id)}
                          disabled={deleteMutation.isPending}
                          className="px-2 py-1 bg-red-600 hover:bg-red-500 rounded-lg text-white text-xs font-medium"
                        >
                          Delete
                        </button>
                        <button
                          onClick={() => setDeleteConfirm(null)}
                          className="px-2 py-1 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-200 text-xs"
                        >
                          No
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setDeleteConfirm(f.id)}
                        className="p-1.5 bg-farm-800 hover:bg-red-900 rounded-lg text-farm-300 hover:text-red-400 transition-colors"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Modals */}
      {showAddModal && (
        <EditFilamentModal
          filament={null}
          onClose={() => setShowAddModal(false)}
          onSave={(data) => {
            const { id, ...rest } = data
            createMutation.mutate(rest)
          }}
        />
      )}

      {editingFilament && (
        <EditFilamentModal
          filament={editingFilament}
          onClose={() => setEditingFilament(null)}
          onSave={updateMutation.mutate}
        />
      )}
    </>
  )
}
