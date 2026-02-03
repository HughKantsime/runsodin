import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Wrench, AlertTriangle, CheckCircle, Clock, Plus, X, Trash2,
  Edit2, ChevronDown, ChevronRight, History, Settings, Activity,
  AlertCircle
} from 'lucide-react'
import { maintenance } from '../api'

const STATUS_COLORS = {
  ok: { bg: 'bg-green-500/10', border: 'border-green-500/30', text: 'text-green-400', dot: 'bg-green-400' },
  due_soon: { bg: 'bg-yellow-500/10', border: 'border-yellow-500/30', text: 'text-yellow-400', dot: 'bg-yellow-400' },
  overdue: { bg: 'bg-red-500/10', border: 'border-red-500/30', text: 'text-red-400', dot: 'bg-red-400' },
}

const STATUS_LABELS = { ok: 'Good', due_soon: 'Due Soon', overdue: 'Overdue' }

function StatusBadge({ status }) {
  const c = STATUS_COLORS[status] || STATUS_COLORS.ok
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${c.bg} ${c.text}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
      {STATUS_LABELS[status] || status}
    </span>
  )
}

function ProgressBar({ percent, status }) {
  const color = status === 'overdue' ? 'bg-red-500' : status === 'due_soon' ? 'bg-yellow-500' : 'bg-green-500'
  return (
    <div className="w-full bg-farm-800 rounded-full h-1.5">
      <div className={`h-1.5 rounded-full ${color} transition-all`} style={{ width: `${Math.min(percent, 100)}%` }} />
    </div>
  )
}

/* ======================== Log Maintenance Modal ======================== */

function LogMaintenanceModal({ printer, task, onClose, onSave, saving }) {
  const [form, setForm] = useState({
    task_name: task?.task_name || '',
    performed_by: '',
    cost: '',
    downtime_minutes: '',
    notes: '',
  })
  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleSubmit = () => {
    onSave({
      printer_id: printer.printer_id,
      task_id: task?.task_id || null,
      task_name: form.task_name,
      performed_by: form.performed_by || null,
      cost: parseFloat(form.cost) || 0,
      downtime_minutes: parseInt(form.downtime_minutes) || 0,
      notes: form.notes || null,
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 border border-farm-700 rounded-lg p-6 w-full max-w-md">
        <div className="flex justify-between items-center mb-4">
          <h3 className="text-lg font-display font-bold text-farm-50">Log Maintenance</h3>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200"><X size={20} /></button>
        </div>
        <div className="text-sm text-farm-400 mb-4">
          <span className="text-farm-200 font-medium">{printer.printer_name}</span> — {task?.task_name || 'Custom task'}
        </div>
        <div className="space-y-3">
          {!task && (
            <div>
              <label className="block text-xs text-farm-400 mb-1">Task Name</label>
              <input className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
                value={form.task_name} onChange={e => set('task_name', e.target.value)} placeholder="e.g. Nozzle Change" />
            </div>
          )}
          <div>
            <label className="block text-xs text-farm-400 mb-1">Performed By</label>
            <input className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
              value={form.performed_by} onChange={e => set('performed_by', e.target.value)} placeholder="Name" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-farm-400 mb-1">Cost ($)</label>
              <input type="number" step="0.01" className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
                value={form.cost} onChange={e => set('cost', e.target.value)} placeholder="0.00" />
            </div>
            <div>
              <label className="block text-xs text-farm-400 mb-1">Downtime (min)</label>
              <input type="number" className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
                value={form.downtime_minutes} onChange={e => set('downtime_minutes', e.target.value)} placeholder="0" />
            </div>
          </div>
          <div>
            <label className="block text-xs text-farm-400 mb-1">Notes</label>
            <textarea className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm" rows={3}
              value={form.notes} onChange={e => set('notes', e.target.value)} placeholder="Any notes about this service..." />
          </div>
        </div>
        <div className="flex gap-2 mt-5">
          <button onClick={onClose} className="flex-1 bg-farm-800 text-farm-300 px-4 py-2 rounded text-sm hover:bg-farm-700">Cancel</button>
          <button onClick={handleSubmit} disabled={!form.task_name || saving}
            className="flex-1 bg-print-500 text-white px-4 py-2 rounded text-sm hover:bg-print-600 disabled:opacity-40">
            {saving ? 'Saving...' : 'Log Service'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ======================== Task Template Form ======================== */

function TaskForm({ task, onSave, onCancel, saving }) {
  const [form, setForm] = useState({
    name: task?.name || '',
    description: task?.description || '',
    printer_model_filter: task?.printer_model_filter || '',
    interval_print_hours: task?.interval_print_hours ?? '',
    interval_days: task?.interval_days ?? '',
    estimated_cost: task?.estimated_cost ?? 0,
    estimated_downtime_min: task?.estimated_downtime_min ?? 30,
  })
  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleSubmit = () => {
    onSave({
      name: form.name,
      description: form.description || null,
      printer_model_filter: form.printer_model_filter || null,
      interval_print_hours: form.interval_print_hours !== '' ? parseFloat(form.interval_print_hours) : null,
      interval_days: form.interval_days !== '' ? parseInt(form.interval_days) : null,
      estimated_cost: parseFloat(form.estimated_cost) || 0,
      estimated_downtime_min: parseInt(form.estimated_downtime_min) || 30,
    })
  }

  return (
    <div className="bg-farm-800/50 border border-farm-700 rounded-lg p-4 space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2">
          <label className="block text-xs text-farm-400 mb-1">Task Name *</label>
          <input className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.name} onChange={e => set('name', e.target.value)} placeholder="e.g. Nozzle Change" />
        </div>
        <div className="sm:col-span-2">
          <label className="block text-xs text-farm-400 mb-1">Description</label>
          <input className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.description} onChange={e => set('description', e.target.value)} placeholder="What this maintenance involves" />
        </div>
        <div>
          <label className="block text-xs text-farm-400 mb-1">Applies To</label>
          <input className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.printer_model_filter} onChange={e => set('printer_model_filter', e.target.value)}
            placeholder="Blank = all printers (e.g. X1, P1S, A1)" />
        </div>
        <div>
          <label className="block text-xs text-farm-400 mb-1">Interval (print hours)</label>
          <input type="number" className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.interval_print_hours} onChange={e => set('interval_print_hours', e.target.value)} placeholder="e.g. 500" />
        </div>
        <div>
          <label className="block text-xs text-farm-400 mb-1">Interval (calendar days)</label>
          <input type="number" className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.interval_days} onChange={e => set('interval_days', e.target.value)} placeholder="e.g. 90" />
        </div>
        <div>
          <label className="block text-xs text-farm-400 mb-1">Est. Cost ($)</label>
          <input type="number" step="0.01" className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100 text-sm"
            value={form.estimated_cost} onChange={e => set('estimated_cost', e.target.value)} />
        </div>
      </div>
      <div className="flex gap-2 pt-1">
        <button onClick={onCancel} className="bg-farm-800 text-farm-300 px-4 py-2 rounded text-sm hover:bg-farm-700">Cancel</button>
        <button onClick={handleSubmit} disabled={!form.name || saving}
          className="bg-print-500 text-white px-4 py-2 rounded text-sm hover:bg-print-600 disabled:opacity-40">
          {saving ? 'Saving...' : task ? 'Update Task' : 'Create Task'}
        </button>
      </div>
    </div>
  )
}

/* ======================== Printer Status Card ======================== */

function PrinterCard({ printer, onLogMaintenance }) {
  const [expanded, setExpanded] = useState(printer.overall_status !== 'ok')
  const colors = STATUS_COLORS[printer.overall_status] || STATUS_COLORS.ok

  return (
    <div className={`border ${colors.border} rounded-lg overflow-hidden`}>
      <div className={`flex items-center justify-between p-4 cursor-pointer hover:bg-farm-800/50 ${colors.bg}`}
        onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center gap-3">
          {expanded ? <ChevronDown size={16} className="text-farm-400" /> : <ChevronRight size={16} className="text-farm-400" />}
          <div>
            <span className="font-medium text-farm-100">{printer.printer_name}</span>
            {printer.printer_model && <span className="text-farm-500 text-sm ml-2">{printer.printer_model}</span>}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-farm-400 font-mono">{printer.total_print_hours}h printed</span>
          <StatusBadge status={printer.overall_status} />
        </div>
      </div>

      {expanded && printer.tasks.length > 0 && (
        <div className="border-t border-farm-800 divide-y divide-farm-800/50">
          {printer.tasks.map(task => (
            <div key={task.task_id} className="flex items-center justify-between px-4 py-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full flex-shrink-0 ${STATUS_COLORS[task.status]?.dot || 'bg-farm-600'}`} />
                  <span className="text-sm text-farm-200">{task.task_name}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 mt-1 ml-4">
                  {task.interval_print_hours != null && (
                    <span className="text-xs text-farm-500">
                      <Clock size={10} className="inline mr-1" />{task.hours_since_service}h / {task.interval_print_hours}h
                    </span>
                  )}
                  {task.interval_days != null && (
                    <span className="text-xs text-farm-500">{task.days_since_service}d / {task.interval_days}d</span>
                  )}
                  {task.last_serviced ? (
                    <span className="text-xs text-farm-600">
                      Last: {new Date(task.last_serviced).toLocaleDateString()}{task.last_by ? ` by ${task.last_by}` : ''}
                    </span>
                  ) : (
                    <span className="text-xs text-farm-600 italic">Never serviced</span>
                  )}
                </div>
                <div className="ml-4 mt-1.5 w-32">
                  <ProgressBar percent={task.progress_percent} status={task.status} />
                </div>
              </div>
              <button onClick={e => { e.stopPropagation(); onLogMaintenance(printer, task) }}
                className="ml-3 bg-farm-800 hover:bg-farm-700 text-farm-300 hover:text-farm-100 px-3 py-1.5 rounded text-xs flex items-center gap-1 flex-shrink-0">
                <CheckCircle size={12} /> Log Service
              </button>
            </div>
          ))}
          {/* Ad-hoc maintenance button */}
          <div className="px-4 py-2">
            <button onClick={() => onLogMaintenance(printer, null)}
              className="text-xs text-farm-500 hover:text-farm-300 flex items-center gap-1">
              <Plus size={12} /> Log ad-hoc maintenance
            </button>
          </div>
        </div>
      )}

      {expanded && printer.tasks.length === 0 && (
        <div className="p-4 text-sm text-farm-500 text-center border-t border-farm-800">
          No maintenance tasks apply to this printer model. Add tasks in the Templates tab.
        </div>
      )}
    </div>
  )
}

/* ======================== Main Page ======================== */

export default function Maintenance() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('status')
  const [logModal, setLogModal] = useState(null)
  const [showTaskForm, setShowTaskForm] = useState(false)
  const [editingTask, setEditingTask] = useState(null)
  const [historyFilter, setHistoryFilter] = useState('')

  // ---- Queries ----
  const { data: statusData = [], isLoading: statusLoading } = useQuery({
    queryKey: ['maintenance', 'status'],
    queryFn: () => maintenance.getStatus(),
  })

  const { data: tasks = [] } = useQuery({
    queryKey: ['maintenance', 'tasks'],
    queryFn: () => maintenance.getTasks(),
  })

  const { data: logs = [] } = useQuery({
    queryKey: ['maintenance', 'logs', historyFilter],
    queryFn: () => maintenance.getLogs(historyFilter || null),
  })

  // ---- Mutations ----
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['maintenance'] })

  const logMutation = useMutation({
    mutationFn: data => maintenance.createLog(data),
    onSuccess: () => { invalidate(); setLogModal(null) },
  })

  const createTaskMutation = useMutation({
    mutationFn: data => maintenance.createTask(data),
    onSuccess: () => { invalidate(); setShowTaskForm(false) },
  })

  const updateTaskMutation = useMutation({
    mutationFn: ({ id, data }) => maintenance.updateTask(id, data),
    onSuccess: () => { invalidate(); setEditingTask(null) },
  })

  const deleteTaskMutation = useMutation({
    mutationFn: id => maintenance.deleteTask(id),
    onSuccess: invalidate,
  })

  const deleteLogMutation = useMutation({
    mutationFn: id => maintenance.deleteLog(id),
    onSuccess: invalidate,
  })

  const seedMutation = useMutation({
    mutationFn: () => maintenance.seedDefaults(),
    onSuccess: invalidate,
  })

  // ---- Stats ----
  const overdueCount = statusData.filter(p => p.overall_status === 'overdue').length
  const dueSoonCount = statusData.filter(p => p.overall_status === 'due_soon').length

  const tabs = [
    { id: 'status', label: 'Fleet Status', icon: Activity },
    { id: 'templates', label: 'Task Templates', icon: Settings },
    { id: 'history', label: 'History', icon: History },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <Wrench className="text-print-400" size={24} />
          <h1 className="text-2xl font-display font-bold text-farm-50">Maintenance</h1>
        </div>
        <div className="flex items-center gap-3">
          {overdueCount > 0 && (
            <span className="bg-red-500/10 text-red-400 px-3 py-1 rounded-full text-sm flex items-center gap-1.5">
              <AlertCircle size={14} /> {overdueCount} overdue
            </span>
          )}
          {dueSoonCount > 0 && (
            <span className="bg-yellow-500/10 text-yellow-400 px-3 py-1 rounded-full text-sm flex items-center gap-1.5">
              <AlertTriangle size={14} /> {dueSoonCount} due soon
            </span>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-farm-900 border border-farm-800 rounded-lg p-1">
        {tabs.map(tab => (
          <button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded text-sm transition-colors ${
              activeTab === tab.id ? 'bg-farm-800 text-farm-100' : 'text-farm-400 hover:text-farm-200 hover:bg-farm-800/50'
            }`}>
            <tab.icon size={14} /> {tab.label}
          </button>
        ))}
      </div>

      {/* ==================== Fleet Status Tab ==================== */}
      {activeTab === 'status' && (
        <div className="space-y-3">
          {statusLoading && <div className="text-farm-500 text-center py-8">Loading fleet status...</div>}

          {!statusLoading && statusData.length === 0 && (
            <div className="text-center py-12 text-farm-500">
              <Wrench size={48} className="mx-auto mb-4 opacity-30" />
              <p className="text-lg mb-2">No printers found</p>
              <p className="text-sm mb-4">Add printers first, then seed default maintenance tasks.</p>
            </div>
          )}

          {!statusLoading && statusData.length > 0 && tasks.length === 0 && (
            <div className="bg-farm-900 border border-farm-800 rounded-lg p-6 text-center">
              <p className="text-farm-400 mb-3">No maintenance tasks configured yet.</p>
              <button onClick={() => seedMutation.mutate()} disabled={seedMutation.isPending}
                className="bg-print-500 hover:bg-print-600 text-white px-6 py-2 rounded text-sm">
                {seedMutation.isPending ? 'Seeding...' : 'Seed Default Tasks for Bambu Printers'}
              </button>
            </div>
          )}

          {statusData.map(printer => (
            <PrinterCard key={printer.printer_id} printer={printer}
              onLogMaintenance={(p, t) => setLogModal({ printer: p, task: t })} />
          ))}
        </div>
      )}

      {/* ==================== Task Templates Tab ==================== */}
      {activeTab === 'templates' && (
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <span className="text-sm text-farm-400">{tasks.length} task template{tasks.length !== 1 ? 's' : ''}</span>
            <div className="flex gap-2">
              <button onClick={() => seedMutation.mutate()} disabled={seedMutation.isPending}
                className="bg-farm-800 hover:bg-farm-700 text-farm-300 px-3 py-1.5 rounded text-sm">
                {seedMutation.isPending ? 'Seeding...' : 'Seed Defaults'}
              </button>
              <button onClick={() => { setShowTaskForm(true); setEditingTask(null) }}
                className="bg-print-500 hover:bg-print-600 text-white px-3 py-1.5 rounded text-sm flex items-center gap-1">
                <Plus size={14} /> Add Task
              </button>
            </div>
          </div>

          {showTaskForm && !editingTask && (
            <TaskForm onSave={data => createTaskMutation.mutate(data)} onCancel={() => setShowTaskForm(false)}
              saving={createTaskMutation.isPending} />
          )}

          <div className="border border-farm-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-farm-900 text-farm-400 text-xs uppercase">
                  <th className="text-left px-4 py-3">Task</th>
                  <th className="text-left px-4 py-3 hidden sm:table-cell">Applies To</th>
                  <th className="text-right px-4 py-3">Hours</th>
                  <th className="text-right px-4 py-3">Days</th>
                  <th className="text-right px-4 py-3 hidden sm:table-cell">Cost</th>
                  <th className="text-right px-4 py-3 w-20">Actions</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map(task => (
                  editingTask?.id === task.id ? (
                    <tr key={task.id}>
                      <td colSpan={6} className="p-2">
                        <TaskForm task={task}
                          onSave={data => updateTaskMutation.mutate({ id: task.id, data })}
                          onCancel={() => setEditingTask(null)}
                          saving={updateTaskMutation.isPending} />
                      </td>
                    </tr>
                  ) : (
                    <tr key={task.id} className="border-t border-farm-800/50 hover:bg-farm-800/30">
                      <td className="px-4 py-3">
                        <div className="text-farm-100">{task.name}</div>
                        {task.description && <div className="text-farm-500 text-xs mt-0.5 max-w-xs truncate">{task.description}</div>}
                      </td>
                      <td className="px-4 py-3 text-farm-400 hidden sm:table-cell">{task.printer_model_filter || 'All'}</td>
                      <td className="px-4 py-3 text-right text-farm-300 font-mono">{task.interval_print_hours ?? '—'}</td>
                      <td className="px-4 py-3 text-right text-farm-300 font-mono">{task.interval_days ?? '—'}</td>
                      <td className="px-4 py-3 text-right text-farm-300 hidden sm:table-cell">{task.estimated_cost ? `$${task.estimated_cost}` : '—'}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center gap-1 justify-end">
                          <button onClick={() => { setEditingTask(task); setShowTaskForm(false) }}
                            className="text-farm-500 hover:text-farm-200 p-1"><Edit2 size={14} /></button>
                          <button onClick={() => { if (confirm(`Delete "${task.name}"?`)) deleteTaskMutation.mutate(task.id) }}
                            className="text-farm-500 hover:text-red-400 p-1"><Trash2 size={14} /></button>
                        </div>
                      </td>
                    </tr>
                  )
                ))}
              </tbody>
            </table>
            {tasks.length === 0 && (
              <div className="p-8 text-center text-farm-500 text-sm">No task templates yet. Click "Seed Defaults" or "Add Task" to get started.</div>
            )}
          </div>
        </div>
      )}

      {/* ==================== History Tab ==================== */}
      {activeTab === 'history' && (
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <select value={historyFilter} onChange={e => setHistoryFilter(e.target.value)}
              className="bg-farm-800 border border-farm-700 rounded px-3 py-1.5 text-farm-200 text-sm">
              <option value="">All Printers</option>
              {statusData.map(p => <option key={p.printer_id} value={p.printer_id}>{p.printer_name}</option>)}
            </select>
            <span className="text-sm text-farm-400">{logs.length} record{logs.length !== 1 ? 's' : ''}</span>
          </div>

          <div className="border border-farm-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-farm-900 text-farm-400 text-xs uppercase">
                  <th className="text-left px-4 py-3">Date</th>
                  <th className="text-left px-4 py-3">Printer</th>
                  <th className="text-left px-4 py-3">Task</th>
                  <th className="text-left px-4 py-3 hidden sm:table-cell">By</th>
                  <th className="text-right px-4 py-3 hidden sm:table-cell">Cost</th>
                  <th className="text-right px-4 py-3 hidden sm:table-cell">Down</th>
                  <th className="text-right px-4 py-3">Hrs@Svc</th>
                  <th className="w-10"></th>
                </tr>
              </thead>
              <tbody>
                {logs.map(log => {
                  const pName = statusData.find(p => p.printer_id === log.printer_id)?.printer_name || `Printer #${log.printer_id}`
                  return (
                    <tr key={log.id} className="border-t border-farm-800/50 hover:bg-farm-800/30">
                      <td className="px-4 py-3 text-farm-300">{log.performed_at ? new Date(log.performed_at).toLocaleDateString() : '—'}</td>
                      <td className="px-4 py-3 text-farm-200">{pName}</td>
                      <td className="px-4 py-3 text-farm-200">
                        {log.task_name}
                        {log.notes && <div className="text-farm-500 text-xs mt-0.5 truncate max-w-xs">{log.notes}</div>}
                      </td>
                      <td className="px-4 py-3 text-farm-400 hidden sm:table-cell">{log.performed_by || '—'}</td>
                      <td className="px-4 py-3 text-right text-farm-300 hidden sm:table-cell">{log.cost ? `$${log.cost}` : '—'}</td>
                      <td className="px-4 py-3 text-right text-farm-300 hidden sm:table-cell">{log.downtime_minutes ? `${log.downtime_minutes}m` : '—'}</td>
                      <td className="px-4 py-3 text-right text-farm-400 font-mono text-xs">{log.print_hours_at_service}h</td>
                      <td className="px-4 py-2 text-right">
                        <button onClick={() => { if (confirm('Delete this log entry?')) deleteLogMutation.mutate(log.id) }}
                          className="text-farm-600 hover:text-red-400 p-1"><Trash2 size={13} /></button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {logs.length === 0 && (
              <div className="p-8 text-center text-farm-500 text-sm">No maintenance history recorded yet. Log service from the Fleet Status tab.</div>
            )}
          </div>
        </div>
      )}

      {/* Log Maintenance Modal */}
      {logModal && (
        <LogMaintenanceModal printer={logModal.printer} task={logModal.task}
          onClose={() => setLogModal(null)}
          onSave={data => logMutation.mutate(data)}
          saving={logMutation.isPending} />
      )}
    </div>
  )
}
