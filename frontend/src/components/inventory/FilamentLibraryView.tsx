import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Palette } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import { filaments as filamentApi } from '../../api'
import { canDo } from '../../permissions'
import { EditFilamentModal } from './SpoolEditModals'

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

  const mutErr = (label) => (err) => toast.error(`${label}: ${err?.message || 'Unknown error'}`)

  const createMutation = useMutation({
    mutationFn: filamentApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setShowAddModal(false)
    },
    onError: mutErr('Create filament failed'),
  })

  const updateMutation = useMutation({
    mutationFn: filamentApi.update,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setEditingFilament(null)
    },
    onError: mutErr('Update filament failed'),
  })

  const deleteMutation = useMutation({
    mutationFn: filamentApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['filaments'] })
      setDeleteConfirm(null)
    },
    onError: mutErr('Delete filament failed'),
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
          <p className="text-[var(--brand-text-muted)] text-sm">{filaments?.length || 0} filament types in library</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={materialFilter}
            onChange={(e) => setMaterialFilter(e.target.value)}
            className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm text-[var(--brand-text-secondary)]"
          >
            <option value="all">All Materials</option>
            {materials.map(m => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {canDo('spools.edit') && (
            <button
              onClick={() => setShowAddModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-[var(--brand-primary)] hover:opacity-90 rounded-md text-white text-sm"
            >
              <Plus size={16} />
              Add Filament
            </button>
          )}
        </div>
      </div>

      {isLoading && <div className="text-center text-[var(--brand-text-muted)] py-12">Loading filaments...</div>}

      {!isLoading && filtered.length === 0 && (
        <div className="text-center text-[var(--brand-text-muted)] py-12 text-sm">
          No filaments found. Add filament types to build your library.
        </div>
      )}

      {/* Filament table grouped by brand */}
      {Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([brand, brandFilaments]) => (
        <div key={brand} className="mb-6">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-2 uppercase tracking-wide">{brand}</h3>
          <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] overflow-hidden">
            {brandFilaments.map((f, idx) => (
              <div
                key={f.id}
                className={clsx(
                  "flex items-center justify-between px-3 md:px-4 py-2.5 md:py-3 gap-3",
                  idx > 0 && "border-t border-[var(--brand-card-border)]"
                )}
              >
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  {f.color_hex ? (
                    <div
                      className="w-3 h-3 rounded-full flex-shrink-0"
                      style={{ backgroundColor: `#${f.color_hex}` }}
                    />
                  ) : (
                    <div className="w-3 h-3 rounded-full border border-[var(--brand-card-border)] bg-[var(--brand-input-bg)] flex-shrink-0 flex items-center justify-center">
                      <Palette size={8} className="text-[var(--brand-text-muted)]" />
                    </div>
                  )}
                  <div className="min-w-0">
                    <span className="text-sm text-[var(--brand-text-primary)] truncate block">{f.name}</span>
                  </div>
                  <span className="px-2 py-0.5 bg-[var(--brand-input-bg)] rounded-md text-xs text-[var(--brand-text-muted)] flex-shrink-0">
                    {f.material}
                  </span>
                  {f.cost_per_gram && (
                    <span className="px-2 py-0.5 bg-green-900/50 rounded-md text-xs text-green-400 flex-shrink-0">
                      ${f.cost_per_gram}/g
                    </span>
                  )}
                </div>

                {canDo('spools.edit') && (
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <button
                      onClick={() => setEditingFilament(f)}
                      className="p-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] transition-colors"
                      title="Edit"
                    >
                      <Pencil size={14} />
                    </button>
                    {deleteConfirm === f.id ? (
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => deleteMutation.mutate(f.id)}
                          disabled={deleteMutation.isPending}
                          className="px-2 py-1 bg-red-600 hover:bg-red-500 rounded-md text-white text-xs font-medium"
                        >
                          Delete
                        </button>
                        <button
                          onClick={() => setDeleteConfirm(null)}
                          className="px-2 py-1 bg-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)] rounded-md text-[var(--brand-text-secondary)] text-xs"
                        >
                          No
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setDeleteConfirm(f.id)}
                        className="p-1.5 bg-[var(--brand-input-bg)] hover:bg-red-900 rounded-md text-[var(--brand-text-secondary)] hover:text-red-400 transition-colors"
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
