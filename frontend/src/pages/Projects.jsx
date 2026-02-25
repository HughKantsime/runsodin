import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { projects } from '../api'
import { canDo } from '../permissions'
import { FolderKanban, Plus, X, Trash2, Archive, ChevronLeft } from 'lucide-react'
import toast from 'react-hot-toast'

const STATUS_BADGES = {
  active: 'bg-green-600/20 text-green-400',
  completed: 'bg-blue-600/20 text-blue-400',
  paused: 'bg-yellow-600/20 text-yellow-400',
  archived: 'bg-farm-700/30 text-farm-400',
}

const STATUS_OPTIONS = ['active', 'completed', 'paused', 'archived']

const DEFAULT_COLORS = [
  '#6366f1', '#8b5cf6', '#ec4899', '#f43f5e', '#f97316',
  '#eab308', '#22c55e', '#14b8a6', '#06b6d4', '#3b82f6',
]

function formatDate(d) {
  if (!d) return '--'
  return new Date(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

function CreateProjectModal({ onClose, onCreate }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [color, setColor] = useState('#6366f1')
  const [expectedParts, setExpectedParts] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!name.trim()) return
    onCreate({
      name: name.trim(),
      description: description.trim() || null,
      color,
      expected_parts: expectedParts ? parseInt(expectedParts) : null,
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-farm-900 rounded-xl border border-farm-800 w-full max-w-md p-4 sm:p-6" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold flex items-center gap-2"><FolderKanban size={18} /> New Project</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-farm-500 mb-1">Name *</label>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
              placeholder="Project name"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-xs text-farm-500 mb-1">Description</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={2}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm resize-none"
              placeholder="Optional description"
            />
          </div>
          <div>
            <label className="block text-xs text-farm-500 mb-1">Color</label>
            <div className="flex flex-wrap gap-2">
              {DEFAULT_COLORS.map(c => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setColor(c)}
                  className={`w-7 h-7 rounded-full border-2 transition-all ${color === c ? 'border-white scale-110' : 'border-transparent'}`}
                  style={{ backgroundColor: c }}
                />
              ))}
            </div>
          </div>
          <div>
            <label className="block text-xs text-farm-500 mb-1">Expected Parts</label>
            <input
              type="number"
              value={expectedParts}
              onChange={e => setExpectedParts(e.target.value)}
              min="0"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
              placeholder="Optional"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-farm-400 hover:text-white transition-colors">Cancel</button>
            <button type="submit" disabled={!name.trim()} className="px-4 py-2 bg-print-600 hover:bg-print-700 rounded-lg text-sm text-white disabled:opacity-50 transition-colors">Create</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ProjectDetail({ project, onClose, onDelete }) {
  const queryClient = useQueryClient()
  const { data: detail, isLoading } = useQuery({
    queryKey: ['project', project.id],
    queryFn: () => projects.get(project.id),
  })

  const updateMutation = useMutation({
    mutationFn: (data) => projects.update(project.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      queryClient.invalidateQueries({ queryKey: ['project', project.id] })
      toast.success('Project updated')
    },
    onError: () => toast.error('Failed to update'),
  })

  const [editStatus, setEditStatus] = useState(null)

  const archives = detail?.archives || []

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-xl border border-farm-800 w-full max-w-lg p-4 sm:p-6 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-4 h-4 rounded-full flex-shrink-0" style={{ backgroundColor: detail?.color || project.color || '#6366f1' }} />
            <h2 className="text-lg font-semibold truncate">{detail?.name || project.name}</h2>
          </div>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        {isLoading ? (
          <div className="text-center py-8 text-farm-500">Loading...</div>
        ) : detail ? (
          <>
            {/* Info grid */}
            <div className="grid grid-cols-2 gap-3 text-sm mb-4">
              <div>
                <span className="text-farm-500 text-xs">Status</span>
                {canDo('printers.edit') ? (
                  <select
                    value={editStatus ?? detail.status}
                    onChange={e => {
                      setEditStatus(e.target.value)
                      updateMutation.mutate({ status: e.target.value })
                    }}
                    className="mt-0.5 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs w-full"
                  >
                    {STATUS_OPTIONS.map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                ) : (
                  <p><span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[detail.status] || 'bg-farm-800 text-farm-400'}`}>{detail.status}</span></p>
                )}
              </div>
              <div>
                <span className="text-farm-500 text-xs">Parts</span>
                <p>{detail.archive_count}{detail.expected_parts ? ` / ${detail.expected_parts}` : ''}</p>
              </div>
              <div>
                <span className="text-farm-500 text-xs">Created</span>
                <p className="text-xs">{formatDate(detail.created_at)}</p>
              </div>
              <div>
                <span className="text-farm-500 text-xs">Updated</span>
                <p className="text-xs">{formatDate(detail.updated_at)}</p>
              </div>
            </div>

            {detail.description && (
              <div className="mb-4">
                <span className="text-farm-500 text-xs">Description</span>
                <p className="text-sm mt-0.5">{detail.description}</p>
              </div>
            )}

            {/* Linked archives */}
            <div className="mb-4">
              <span className="text-farm-500 text-xs">Linked Archives ({archives.length})</span>
              {archives.length === 0 ? (
                <p className="text-sm text-farm-500 mt-1">No archives linked yet</p>
              ) : (
                <div className="mt-1 space-y-1 max-h-[200px] overflow-y-auto">
                  {archives.map(a => (
                    <div key={a.id} className="flex items-center gap-2 px-2 py-1.5 bg-farm-800/50 rounded text-xs">
                      <Archive size={12} className="text-farm-500 flex-shrink-0" />
                      <span className="truncate flex-1">{a.print_name || `Archive #${a.id}`}</span>
                      <span className="text-farm-500">{a.printer_nickname || a.printer_name || '--'}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Delete */}
            {canDo('printers.delete') && (
              <button
                onClick={() => { onDelete(project.id); onClose() }}
                className="w-full py-2 rounded-lg text-sm text-red-400 bg-red-600/10 hover:bg-red-600/20 transition-colors flex items-center justify-center gap-2"
              >
                <Trash2 size={14} /> Archive Project
              </button>
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}

export default function ProjectsPage() {
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [selected, setSelected] = useState(null)
  const [filterStatus, setFilterStatus] = useState('')

  const { data: projectList, isLoading } = useQuery({
    queryKey: ['projects', filterStatus],
    queryFn: () => projects.list(filterStatus || undefined),
  })

  const createMutation = useMutation({
    mutationFn: (data) => projects.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setShowCreate(false)
      toast.success('Project created')
    },
    onError: () => toast.error('Failed to create project'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => projects.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      toast.success('Project archived')
    },
    onError: () => toast.error('Failed to archive project'),
  })

  const items = projectList || []

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 md:mb-6">
        <div className="flex items-center gap-3">
          <FolderKanban className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Projects</h1>
            <p className="text-farm-500 text-sm mt-1">{items.length} project{items.length !== 1 ? 's' : ''}</p>
          </div>
        </div>
        {canDo('printers.edit') && (
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 px-3 py-2 bg-print-600 hover:bg-print-700 rounded-lg text-sm text-white transition-colors"
          >
            <Plus size={16} /> New
          </button>
        )}
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 mb-4">
        <select
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          {STATUS_OPTIONS.map(s => (
            <option key={s} value={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</option>
          ))}
        </select>
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading...</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <FolderKanban size={40} className="mx-auto mb-3 opacity-30" />
          <p>No projects yet</p>
          <p className="text-xs mt-1">Create a project to group related print archives</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {items.map(project => (
            <button
              key={project.id}
              onClick={() => setSelected(project)}
              className="bg-farm-900 rounded-lg border border-farm-800 p-4 text-left hover:border-farm-700 transition-colors"
            >
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2 min-w-0">
                  <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ backgroundColor: project.color || '#6366f1' }} />
                  <span className="font-medium text-sm truncate">{project.name}</span>
                </div>
                <span className={`px-2 py-0.5 rounded text-[10px] flex-shrink-0 ml-2 ${STATUS_BADGES[project.status] || 'bg-farm-800 text-farm-400'}`}>
                  {project.status}
                </span>
              </div>
              {project.description && (
                <p className="text-xs text-farm-500 mb-3 line-clamp-2">{project.description}</p>
              )}
              <div className="flex items-center justify-between text-xs text-farm-400 mt-auto pt-2 border-t border-farm-800">
                <span className="flex items-center gap-1">
                  <Archive size={12} />
                  {project.archive_count || 0} part{(project.archive_count || 0) !== 1 ? 's' : ''}
                  {project.expected_parts ? ` / ${project.expected_parts}` : ''}
                </span>
                <span>{formatDate(project.updated_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}

      {/* Create modal */}
      {showCreate && (
        <CreateProjectModal
          onClose={() => setShowCreate(false)}
          onCreate={(data) => createMutation.mutate(data)}
        />
      )}

      {/* Detail modal */}
      {selected && (
        <ProjectDetail
          project={selected}
          onClose={() => setSelected(null)}
          onDelete={(id) => deleteMutation.mutate(id)}
        />
      )}
    </div>
  )
}
