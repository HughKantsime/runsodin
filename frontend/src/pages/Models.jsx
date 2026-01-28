import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { 
  Plus, 
  Pencil, 
  Trash2,
  Clock,
  Palette,
  DollarSign
} from 'lucide-react'
import clsx from 'clsx'

import { models } from '../api'

function ModelCard({ model, onEdit, onDelete }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden hover:border-farm-700 transition-colors">
      {/* Thumbnail placeholder */}
      <div className="h-32 bg-farm-800 flex items-center justify-center">
        {model.thumbnail_url ? (
          <img 
            src={model.thumbnail_url} 
            alt={model.name}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="text-4xl text-farm-700">🖨️</div>
        )}
      </div>

      {/* Content */}
      <div className="p-4">
        <div className="flex items-start justify-between mb-2">
          <div>
            <h3 className="font-medium">{model.name}</h3>
            {model.category && (
              <span className="text-xs text-farm-500">{model.category}</span>
            )}
          </div>
          
          <div className="flex items-center gap-1">
            <button
              onClick={() => onEdit(model)}
              className="p-1.5 text-farm-400 hover:bg-farm-800 rounded transition-colors"
            >
              <Pencil size={14} />
            </button>
            <button
              onClick={() => onDelete(model.id)}
              className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded transition-colors"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>

        {/* Stats */}
        <div className="flex items-center gap-4 text-sm text-farm-400 mt-3">
          {model.build_time_hours && (
            <div className="flex items-center gap-1">
              <Clock size={14} />
              <span>{model.build_time_hours}h</span>
            </div>
          )}
          {model.required_colors?.length > 0 && (
            <div className="flex items-center gap-1">
              <Palette size={14} />
              <span>{model.required_colors.length} colors</span>
            </div>
          )}
          {model.cost_per_item && (
            <div className="flex items-center gap-1">
              <DollarSign size={14} />
              <span>${model.cost_per_item.toFixed(2)}</span>
            </div>
          )}
        </div>

        {/* Colors */}
        {model.required_colors?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-3">
            {model.required_colors.map((color, i) => (
              <span 
                key={i}
                className="text-xs bg-farm-800 px-2 py-0.5 rounded"
              >
                {color}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ModelModal({ isOpen, onClose, onSubmit, editingModel }) {
  const [formData, setFormData] = useState({
    name: editingModel?.name || '',
    category: editingModel?.category || '',
    build_time_hours: editingModel?.build_time_hours || '',
    cost_per_item: editingModel?.cost_per_item || '',
    notes: editingModel?.notes || '',
    // Simplified color input as comma-separated string
    colors: editingModel?.required_colors?.join(', ') || '',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    
    // Parse colors into color_requirements format
    const colorList = formData.colors.split(',').map(c => c.trim()).filter(Boolean)
    const color_requirements = {}
    colorList.forEach((color, i) => {
      color_requirements[`color${i + 1}`] = { color, grams: 0 }
    })

    onSubmit({
      name: formData.name,
      category: formData.category || null,
      build_time_hours: formData.build_time_hours ? Number(formData.build_time_hours) : null,
      cost_per_item: formData.cost_per_item ? Number(formData.cost_per_item) : null,
      notes: formData.notes || null,
      color_requirements: Object.keys(color_requirements).length > 0 ? color_requirements : null,
    }, editingModel?.id)
    
    setFormData({
      name: '',
      category: '',
      build_time_hours: '',
      cost_per_item: '',
      notes: '',
      colors: '',
    })
    onClose()
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-xl w-full max-w-lg p-6 border border-farm-700">
        <h2 className="text-xl font-display font-semibold mb-4">
          {editingModel ? 'Edit Model' : 'Add New Model'}
        </h2>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Model Name *</label>
            <input
              type="text"
              required
              value={formData.name}
              onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
              placeholder="e.g., Crocodile (Mini Critter)"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Category</label>
              <input
                type="text"
                value={formData.category}
                onChange={(e) => setFormData(prev => ({ ...prev, category: e.target.value }))}
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
                placeholder="e.g., Mini Critters"
              />
            </div>
            
            <div>
              <label className="block text-sm text-farm-400 mb-1">Build Time (hours)</label>
              <input
                type="number"
                step="0.5"
                min="0"
                value={formData.build_time_hours}
                onChange={(e) => setFormData(prev => ({ ...prev, build_time_hours: e.target.value }))}
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
                placeholder="e.g., 15.5"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Colors Required</label>
            <input
              type="text"
              value={formData.colors}
              onChange={(e) => setFormData(prev => ({ ...prev, colors: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
              placeholder="e.g., black, white, green matte, yellow matte"
            />
            <p className="text-xs text-farm-500 mt-1">Comma-separated list of colors</p>
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Cost per Item ($)</label>
            <input
              type="number"
              step="0.01"
              min="0"
              value={formData.cost_per_item}
              onChange={(e) => setFormData(prev => ({ ...prev, cost_per_item: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
              placeholder="e.g., 5.50"
            />
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea
              value={formData.notes}
              onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 h-20"
              placeholder="Optional notes..."
            />
          </div>

          <div className="flex justify-end gap-3 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors"
            >
              {editingModel ? 'Save Changes' : 'Add Model'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function Models() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [editingModel, setEditingModel] = useState(null)
  const [categoryFilter, setCategoryFilter] = useState('')

  const { data: modelsData, isLoading } = useQuery({
    queryKey: ['models', categoryFilter],
    queryFn: () => models.list(categoryFilter || null),
  })

  const createModel = useMutation({
    mutationFn: models.create,
    onSuccess: () => queryClient.invalidateQueries(['models']),
  })

  const updateModel = useMutation({
    mutationFn: ({ id, data }) => models.update(id, data),
    onSuccess: () => queryClient.invalidateQueries(['models']),
  })

  const deleteModel = useMutation({
    mutationFn: models.delete,
    onSuccess: () => queryClient.invalidateQueries(['models']),
  })

  const handleSubmit = (data, modelId) => {
    if (modelId) {
      updateModel.mutate({ id: modelId, data })
    } else {
      createModel.mutate(data)
    }
  }

  const handleEdit = (model) => {
    setEditingModel(model)
    setShowModal(true)
  }

  const handleDelete = (modelId) => {
    if (confirm('Delete this model? Jobs using this model will not be affected.')) {
      deleteModel.mutate(modelId)
    }
  }

  // Get unique categories
  const categories = [...new Set(modelsData?.map(m => m.category).filter(Boolean))]

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-display font-bold">Models</h1>
          <p className="text-farm-500 mt-1">Print model library</p>
        </div>
        
        <button
          onClick={() => {
            setEditingModel(null)
            setShowModal(true)
          }}
          className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors"
        >
          <Plus size={18} />
          Add Model
        </button>
      </div>

      {/* Category Filter */}
      {categories.length > 0 && (
        <div className="flex gap-2 mb-6 flex-wrap">
          <button
            onClick={() => setCategoryFilter('')}
            className={clsx(
              'px-3 py-1.5 rounded-lg text-sm transition-colors',
              !categoryFilter 
                ? 'bg-print-600 text-white' 
                : 'bg-farm-800 text-farm-400 hover:bg-farm-700'
            )}
          >
            All
          </button>
          {categories.map((cat) => (
            <button
              key={cat}
              onClick={() => setCategoryFilter(cat)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-sm transition-colors',
                categoryFilter === cat 
                  ? 'bg-print-600 text-white' 
                  : 'bg-farm-800 text-farm-400 hover:bg-farm-700'
              )}
            >
              {cat}
            </button>
          ))}
        </div>
      )}

      {/* Models Grid */}
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading models...</div>
      ) : modelsData?.length === 0 ? (
        <div className="bg-farm-900 rounded-xl border border-farm-800 p-12 text-center">
          <p className="text-farm-500 mb-4">No models defined yet.</p>
          <p className="text-sm text-farm-600 mb-4">
            Models store default settings for prints you make frequently.
          </p>
          <button
            onClick={() => setShowModal(true)}
            className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors"
          >
            Add Your First Model
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {modelsData?.map((model) => (
            <ModelCard
              key={model.id}
              model={model}
              onEdit={handleEdit}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      {/* Modal */}
      <ModelModal
        isOpen={showModal}
        onClose={() => {
          setShowModal(false)
          setEditingModel(null)
        }}
        onSubmit={handleSubmit}
        editingModel={editingModel}
      />
    </div>
  )
}
